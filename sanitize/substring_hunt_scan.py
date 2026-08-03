"""
Step 6: full-database substring-hunt scan.

Checks every text/varchar/jsonb column in every table in the public schema
(true brute force -- not limited to the 7 canonical source models) for any
occurrence of a known real PII value, using a single Aho-Corasick automaton
built from all 13.5k hunt-set patterns simultaneously (one pass per value,
regardless of pattern count -- this is the speed-critical piece; 13.5k
separate LIKE queries per column would be far slower).

Any hit gets checked against a persisted KNOWN_DESTINATIONS registry:
- known (table, column) -> auto-splice the transformed value in, write
  immediately via raw SQL (safe here since hr_payslip.name is confirmed
  compute=False/store=True, a real independent column).
- novel (table, column) never seen before -> flagged for review, NOT
  auto-fixed.

Parallelized: one thread per table, each with its own psycopg2 connection.

Debug-by-default: prints every hit found, every auto-fix applied, every
novel flag raised.
"""
import csv
import hashlib
import hmac
import json
import os
import re
import string
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import ahocorasick
import psycopg2

from pii_registry import KNOWN_DESTINATIONS, EXCLUDE_DESTINATIONS, EXCLUDE_TABLES_PREFIX

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"
_LOWER = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits


def _keystream(value, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(SECRET_KEY, value.encode("utf-8") + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def transform_plain(value):
    if not value:
        return value
    ks = _keystream(value, len(value))
    out = []
    for ch, kb in zip(value, ks):
        if ch.isdigit():
            out.append(_DIGITS[kb % 10])
        elif ch.isalpha():
            out.append(_UPPER[kb % 26] if ch.isupper() else _LOWER[kb % 26])
        else:
            out.append(ch)
    return "".join(out)

# Resolved from env vars set by run_pipeline.sh (client-parameterized) --
# fall back to the orion_test-specific literals for standalone runs.
DB_HOST = os.environ.get("DBHOST", "db_orion_test")
DB_NAME = os.environ.get("SANITIZED_DB_NAME", "orm_test")
DB_USER = "odoo"
DB_PASSWORD = os.environ.get("DBPASS", "F0aclHkVKiTxFwCHsf6UoS26")

# Scoped by SOURCE_DB_NAME (the hunt-set is keyed by real values from the
# source, not the sanitized target) -- see build_pii_dictionary.py's
# OUTPUT_CSV comment for why an unscoped shared path is unsafe.
_SOURCE_DB_NAME = os.environ.get("SOURCE_DB_NAME", "orion_test")
HITS_CSV = f"/tmp/substring_hunt_hits_{_SOURCE_DB_NAME}.csv"


def load_hunt_set():
    values = []
    with open(f"/tmp/pii_dictionary_substring_hunt_{_SOURCE_DB_NAME}.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            values.append(row["value"])
    return values


def build_automaton(hunt_values):
    """Case-insensitive: built on lowercased patterns. A real casing variant
    that was never in the original dictionary (e.g. 'sarthak samgir' when
    only 'Sarthak Samgir' and 'Sarthak samgir' were captured) would
    otherwise silently evade an exact-match scan -- found live via a "Dear
    sarthak samgir," chatter notification that slipped through. Lowercasing
    the pattern side catches any casing of a known name/value."""
    A = ahocorasick.Automaton()
    for v in set(hunt_values):
        if v:
            A.add_word(v.lower(), v.lower())
    A.make_automaton()
    return A


def splice_replace(value, matches):
    """matches: list of (end_index, matched_lowercase_string) from
    Aho-Corasick run against value.lower(). Recomputes transform_plain
    fresh on the ACTUAL matched span from the original (correctly-cased)
    text, rather than looking up a precomputed mapping -- this way any
    casing variant is handled correctly without needing to have anticipated
    that exact casing in the dictionary ahead of time."""
    spans = []
    for end_idx, matched_lower in matches:
        start_idx = end_idx - len(matched_lower) + 1
        spans.append((start_idx, end_idx))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))  # by start, longest first
    result = []
    last_end = -1
    for start, end in spans:
        if start <= last_end:
            continue  # overlapping with a previous (longer) match, skip
        result.append(value[last_end + 1:start])
        actual_span = value[start:end + 1]
        result.append(transform_plain(actual_span))
        last_end = end
    result.append(value[last_end + 1:])
    return "".join(result)


def get_all_text_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND data_type IN ('text', 'character varying', 'jsonb')
            ORDER BY table_name, column_name
            """
        )
        return cur.fetchall()


def scan_table(table, columns, automaton):
    """columns: list of (column_name, data_type) for this table."""
    t0 = time.time()
    hits = []
    try:
        conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    except Exception as e:
        print(f"[{table}] ERROR connecting: {e}")
        return table, hits

    try:
        with conn.cursor() as cur:
            for col_name, data_type in columns:
                try:
                    if data_type == "jsonb":
                        cur.execute(
                            f'SELECT id, "{col_name}" FROM "{table}" WHERE "{col_name}" IS NOT NULL'
                        )
                    else:
                        cur.execute(
                            f'SELECT id, "{col_name}" FROM "{table}" WHERE "{col_name}" IS NOT NULL '
                            f'AND "{col_name}" != \'\''
                        )
                except Exception as e:
                    print(f"[{table}.{col_name}] query failed, skipping: {e}")
                    conn.rollback()
                    continue

                rows = cur.fetchall()
                for row_id, val in rows:
                    if data_type == "jsonb":
                        try:
                            d = val if isinstance(val, dict) else json.loads(val)
                        except Exception:
                            continue
                        for lang, text in (d or {}).items():
                            if not isinstance(text, str):
                                continue
                            matches = list(automaton.iter(text.lower()))
                            if matches:
                                hits.append((table, col_name, row_id, text, matches, lang))
                    else:
                        if not isinstance(val, str) or not val:
                            continue
                        matches = list(automaton.iter(val.lower()))
                        if matches:
                            hits.append((table, col_name, row_id, val, matches, None))
    except Exception as e:
        print(f"[{table}] ERROR during scan: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    print(f"[{table}] scanned in {time.time()-t0:.2f}s, {len(hits)} hits")
    return table, hits


def main():
    hunt_values = load_hunt_set()
    print(f"Building Aho-Corasick automaton from {len(hunt_values)} hunt patterns...")
    automaton = build_automaton(hunt_values)
    print("Automaton built.")

    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    all_columns = get_all_text_columns(conn)
    conn.close()

    by_table = {}
    for table, col, dtype in all_columns:
        if table.startswith(EXCLUDE_TABLES_PREFIX):
            continue
        by_table.setdefault(table, []).append((col, dtype))

    print(f"Scanning {len(by_table)} tables, {sum(len(v) for v in by_table.values())} columns total...")

    all_hits = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(scan_table, t, cols, automaton): t for t, cols in by_table.items()}
        for fut in as_completed(futures):
            table = futures[fut]
            try:
                _, hits = fut.result()
                all_hits.extend(hits)
            except Exception as e:
                print(f"[{table}] FAILED: {e}")
                traceback.print_exc()

    print(f"\nTotal raw hits across whole database: {len(all_hits)}")

    known_hits = [h for h in all_hits if (h[0], h[1]) in KNOWN_DESTINATIONS]
    excluded_hits = [h for h in all_hits if (h[0], h[1]) in EXCLUDE_DESTINATIONS]
    novel_hits = [
        h for h in all_hits
        if (h[0], h[1]) not in KNOWN_DESTINATIONS and (h[0], h[1]) not in EXCLUDE_DESTINATIONS
    ]

    print(f"Known-destination hits (auto-fixable): {len(known_hits)}")
    print(f"Excluded (reference/technical, not identity): {len(excluded_hits)}")
    print(f"NOVEL (table, column) hits -- needs review, not auto-fixed: {len(novel_hits)}")

    novel_tables_cols = sorted(set((h[0], h[1]) for h in novel_hits))
    print(f"\n=== Novel (table, column) pairs requiring review ({len(novel_tables_cols)}) ===")
    for t, c in novel_tables_cols:
        sample = next(h for h in novel_hits if h[0] == t and h[1] == c)
        print(f"  {t}.{c} -- e.g. row {sample[2]}: {sample[3]!r}")

    # write out full hit report for review
    with open(HITS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "row_id", "value", "matched_patterns", "known_destination"])
        for table, col, row_id, val, matches, lang in all_hits:
            matched_strs = "|".join(sorted(set(m for _, m in matches)))
            is_known = (table, col) in KNOWN_DESTINATIONS
            w.writerow([table, col, row_id, val, matched_strs, is_known])
    print(f"Wrote {HITS_CSV}")

    # auto-fix known destinations. jsonb (translated) columns need proper
    # json reconstruction, not a raw string UPDATE -- fetch the current full
    # dict fresh each time so sequential per-language fixes on the same row
    # don't clobber each other.
    if known_hits:
        conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
        fixed = 0
        errors = 0
        with conn.cursor() as cur:
            for table, col, row_id, val, matches, lang in known_hits:
                try:
                    new_val = splice_replace(val, matches)
                    if lang is not None:
                        cur.execute(f'SELECT "{col}" FROM "{table}" WHERE id = %s', (row_id,))
                        raw = cur.fetchone()[0]
                        d = raw if isinstance(raw, dict) else json.loads(raw)
                        d[lang] = new_val
                        cur.execute(f'UPDATE "{table}" SET "{col}" = %s WHERE id = %s', (json.dumps(d), row_id))
                    else:
                        cur.execute(f'UPDATE "{table}" SET "{col}" = %s WHERE id = %s', (new_val, row_id))
                    conn.commit()
                    fixed += 1
                    print(f"AUTO-FIXED {table}.{col}(id={row_id}, lang={lang}): {val!r} -> {new_val!r}")
                except Exception as e:
                    conn.rollback()
                    errors += 1
                    print(f"ERROR auto-fixing {table}.{col}(id={row_id}): {e}")
        conn.close()
        print(f"\nAuto-fix done: {fixed} fixed, {errors} errors")


if __name__ == "__main__":
    main()
