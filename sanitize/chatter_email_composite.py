"""
Step 6c: fix for the composite '"Name" <email>' format used by
mail_message.email_from, mail_mail.email_to, mail_mail.email_cc --
exact-value lookup against pii_value_mapping.csv never matches these,
since the dictionary holds bare names and bare emails separately, not the
combined formatted string Odoo actually stores.

Parses each DISTINCT value in these 3 fields, looks up the name part and
email part independently against the existing mapping (built from
res.partner/hr.employee/res.users etc.), reconstructs '"MappedName"
<mapped_email>' so the same real person maps to the same fake identity
here as everywhere else in the DB. Falls back to a generic placeholder
piece only when a part isn't found in the dictionary at all (e.g.
OdooBot, catchall@, or a company-level email with no personal name).

Builds one (original -> reconstructed) mapping for the distinct values
actually present, then applies it via the same single bulk-join UPDATE
pattern as chatter_bulk.py -- still no per-row ORM writes.

Debug-by-default: prints every distinct value's parse + lookup result.
"""
import csv
import re
import time

import psycopg2
from psycopg2.extras import execute_values

DB_HOST = "db_orion_test"
DB_NAME = "orm_test"
DB_USER = "odoo"
DB_PASSWORD = "F0aclHkVKiTxFwCHsf6UoS26"

MAPPING_CSV = "/tmp/pii_value_mapping.csv"

FIELDS = [
    ("mail_message", "email_from"),
    ("mail_mail", "email_to"),
    ("mail_mail", "email_cc"),
]

FALLBACK_NAME = "Test Person"
FALLBACK_EMAIL = "test@example.test"

_COMPOSITE_RE = re.compile(r'^\s*"([^"]*)"\s*<([^>]*)>\s*$')
_BARE_EMAIL_RE = re.compile(r'^[^<>"]+@[^<>"]+$')


def parse(value):
    """Returns (name_or_None, email_or_None) or (None, None) if unparseable."""
    m = _COMPOSITE_RE.match(value)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if _BARE_EMAIL_RE.match(value.strip()):
        return None, value.strip()
    return None, None


def build_reconstruction(distinct_values, name_map, email_map):
    out = {}
    unparsed = 0
    for value in distinct_values:
        name, email = parse(value)
        if name is None and email is None:
            unparsed += 1
            continue
        new_name = name_map.get(name, FALLBACK_NAME) if name else None
        new_email = email_map.get(email, FALLBACK_EMAIL) if email else FALLBACK_EMAIL
        if name is not None:
            out[value] = f'"{new_name}" <{new_email}>'
        else:
            out[value] = new_email
    print(f"  parsed {len(out)} of {len(distinct_values)} distinct values ({unparsed} unparseable, left untouched)")
    return out


def main():
    t0 = time.time()
    name_map = {}
    email_map = {}
    with open(MAPPING_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            orig, trans = row["original_value"], row["transformed_value"]
            if "@" in orig:
                email_map[orig] = trans
            else:
                name_map[orig] = trans
    print(f"Loaded mapping: {len(name_map)} name entries, {len(email_map)} email entries")

    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()

    for table, column in FIELDS:
        print(f"\n=== {table}.{column} ===")
        cur.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL')
        distinct_values = [r[0] for r in cur.fetchall()]
        recon = build_reconstruction(distinct_values, name_map, email_map)
        if not recon:
            print("  nothing to update")
            continue

        cur.execute("DROP TABLE IF EXISTS recon_map")
        cur.execute("CREATE TEMP TABLE recon_map (original text PRIMARY KEY, transformed text)")
        execute_values(
            cur,
            "INSERT INTO recon_map (original, transformed) VALUES %s ON CONFLICT DO NOTHING",
            list(recon.items()),
        )
        cur.execute(
            f'UPDATE "{table}" t SET "{column}" = m.transformed '
            f'FROM recon_map m WHERE t."{column}" = m.original'
        )
        print(f"  {cur.rowcount} rows updated")
        conn.commit()

    print(f"\n=== Done in {time.time()-t0:.2f}s total ===")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
