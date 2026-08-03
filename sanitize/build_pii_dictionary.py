"""
Canonical PII value dictionary builder — pure psycopg2, no ORM/registry
overhead (avoids odoo shell's ~2s registry load, irrelevant for a pure
SQL-read task). Pulls EVERY text/varchar column's distinct values from the
widened canonical identity-source tables, including archived rows (raw SQL
has no ORM active-record filter) and all companies (no company_id filter
applied at all).

Parallelized: one thread per table, each with its own psycopg2 connection
(connections aren't thread-safe to share). Small workload here (~7 tables),
but this is the same pattern reused for the much bigger full-database
verification scan later, where the parallelism actually pays off.

Debug-by-default: prints progress/counts per table, never swallows errors
silently.
"""
import csv
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2

from pii_registry import CANONICAL_TABLES

# Resolved from env vars set by run_pipeline.sh (client-parameterized) --
# fall back to the original orion_test-specific literals for standalone
# runs outside the pipeline.
DB_HOST = os.environ.get("DBHOST", "db_orion_test")
DB_NAME = os.environ.get("SOURCE_DB_NAME", "orion_test")  # real source, not the scratch/sanitized copy
DB_USER = "odoo"
DB_PASSWORD = os.environ.get("DBPASS", "F0aclHkVKiTxFwCHsf6UoS26")

# Scoped by SOURCE_DB_NAME (client-specific) -- an unscoped shared path
# caused a real PermissionError on parus_instruments's first run: a stale
# file left over from an earlier manual `docker cp` (different owning uid)
# blocked a later run from overwriting it. Client-scoped paths also
# protect against genuinely concurrent/interleaved runs of two different
# clients, not just accidental leftovers.
OUTPUT_CSV = f"/tmp/pii_value_dictionary_{DB_NAME}.csv"


def get_text_columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
              AND data_type IN ('text', 'character varying', 'jsonb')
            ORDER BY column_name
            """,
            (table,),
        )
        return cur.fetchall()


def dump_table(table):
    """Runs in its own thread with its own connection."""
    t0 = time.time()
    try:
        conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    except Exception as e:
        print(f"[{table}] ERROR connecting: {e}")
        traceback.print_exc()
        return table, []

    rows_out = []
    try:
        columns = get_text_columns(conn, table)
        print(f"[{table}] {len(columns)} text-like columns: {[c[0] for c in columns]}")
        with conn.cursor() as cur:
            for col_name, data_type in columns:
                if data_type == "jsonb":
                    # translated fields store as jsonb {'en_US': 'value', ...}
                    # pull the raw jsonb text representation's values, not the whole blob
                    try:
                        cur.execute(
                            f'SELECT DISTINCT jsonb_each_text("{col_name}") FROM "{table}" '
                            f'WHERE "{col_name}" IS NOT NULL'
                        )
                    except Exception as e:
                        print(f"[{table}.{col_name}] jsonb query failed, skipping: {e}")
                        conn.rollback()
                        continue
                else:
                    try:
                        cur.execute(
                            f'SELECT DISTINCT "{col_name}" FROM "{table}" '
                            f'WHERE "{col_name}" IS NOT NULL AND "{col_name}" != \'\''
                        )
                    except Exception as e:
                        print(f"[{table}.{col_name}] query failed, skipping: {e}")
                        conn.rollback()
                        continue
                vals = cur.fetchall()
                for v in vals:
                    val = v[0]
                    if data_type == "jsonb" and isinstance(val, (list, tuple)):
                        val = val[-1] if len(val) > 1 else val[0]
                    if val:
                        rows_out.append((table, col_name, str(val)))
                print(f"[{table}.{col_name}] {len(vals)} distinct values")
    except Exception as e:
        print(f"[{table}] ERROR during dump: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    print(f"[{table}] done in {time.time()-t0:.2f}s, {len(rows_out)} total values")
    return table, rows_out


def main():
    print(f"Dumping canonical PII dictionary from {len(CANONICAL_TABLES)} tables (parallel)...")
    all_rows = []
    with ThreadPoolExecutor(max_workers=len(CANONICAL_TABLES)) as ex:
        futures = {ex.submit(dump_table, t): t for t in CANONICAL_TABLES}
        for fut in as_completed(futures):
            table = futures[fut]
            try:
                _, rows = fut.result()
                all_rows.extend(rows)
            except Exception as e:
                print(f"[{table}] FAILED: {e}")
                traceback.print_exc()

    print(f"Total raw values collected: {len(all_rows)}")
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["table", "column", "value"])
        writer.writerows(all_rows)
    print(f"Wrote {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
