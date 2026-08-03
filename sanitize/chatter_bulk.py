"""
Step 6b: chatter/communication-log sanitization, done as pure bulk SQL --
no ORM, no per-row scanning. Two buckets, one statement per field:

BUCKET 1 -- flat placeholder (whole field is free text, no consistency
requirement, nothing downstream reads it as "the real value"):
  mail.message.body/subject, mail.mail.body_html, mail.activity.note/summary,
  sms.sms.body, mail.tracking.value old/new_value_char/text.
  A single UPDATE ... SET col = placeholder WHERE col IS NOT NULL per field.

BUCKET 2 -- exact-value bulk-mapped join (whole field IS a single value --
an email, phone, or name -- that should map to the SAME fake value used
everywhere else in the DB, via the existing pii_value_mapping.csv):
  mail.message.email_from/reply_to/record_name, mail.mail.email_to/email_cc,
  mail.activity.res_name, mail.notification.sms_number, sms.sms.number.
  Loads the mapping into a temp table once, then a single
  UPDATE t SET col = m.transformed FROM pii_map m WHERE t.col = m.original
  per field, with an unmapped-fallback UPDATE for anything not in the
  dictionary (still needs SOME replacement, just not a consistent one).

Runs directly against orm_test via psycopg2 -- no odoo shell, no ORM
registry load, no per-row Python loop. Everything here is either
config/state data with no compute dependents, or content nobody
recomputes from -- confirmed via information_schema, no compute=True
fields in this list.

Debug-by-default: prints every statement executed and its row count.
"""
import csv
import os
import time

import psycopg2

# Resolved from env vars set by run_pipeline.sh (client-parameterized) --
# fall back to the orion_test-specific literals for standalone runs.
DB_HOST = os.environ.get("DBHOST", "db_orion_test")
DB_NAME = os.environ.get("SANITIZED_DB_NAME", "orm_test")
DB_USER = "odoo"
DB_PASSWORD = os.environ.get("DBPASS", "F0aclHkVKiTxFwCHsf6UoS26")

MAPPING_CSV = "/tmp/pii_value_mapping.csv"

FLAT_PLACEHOLDER_FIELDS = [
    ("mail_message", "body", "<p>Test message content.</p>"),
    ("mail_message", "subject", "Test subject"),
    ("mail_mail", "body_html", "<p>Test message content.</p>"),
    ("mail_activity", "note", "<p>Test activity note.</p>"),
    ("mail_activity", "summary", "Test activity"),
    ("sms_sms", "body", "Test SMS content."),
    ("mail_tracking_value", "old_value_char", "old-test-value"),
    ("mail_tracking_value", "new_value_char", "new-test-value"),
    ("mail_tracking_value", "old_value_text", "old-test-value"),
    ("mail_tracking_value", "new_value_text", "new-test-value"),
]

# (table, column, fallback placeholder for unmapped values)
# NOTE: mail_message.email_from and mail_mail.email_to/email_cc are
# deliberately NOT here -- Odoo stores them as composite '"Name" <email>'
# strings, which never exact-match the bare-value dictionary. Those 3
# fields are handled exclusively by chatter_email_composite.py, which must
# run BEFORE this script (or on fresh, unclobbered data).
EXACT_MAPPED_FIELDS = [
    ("mail_message", "reply_to", "test@example.test"),
    ("mail_message", "record_name", "Test record"),
    ("mail_activity", "res_name", "Test record"),
    ("mail_notification", "sms_number", "+910000000000"),
    ("sms_sms", "number", "+910000000000"),
]


def main():
    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    conn.autocommit = False
    cur = conn.cursor()
    t0 = time.time()

    print("=== Bucket 1: flat placeholder fields ===")
    for table, column, placeholder in FLAT_PLACEHOLDER_FIELDS:
        try:
            cur.execute(
                f'UPDATE "{table}" SET "{column}" = %s '
                f'WHERE "{column}" IS NOT NULL AND "{column}" != %s',
                (placeholder, placeholder),
            )
            print(f"[{table}.{column}] {cur.rowcount} rows updated")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"ERROR on {table}.{column}: {type(e).__name__}: {e}")

    print("\n=== Loading mapping into temp table ===")
    cur.execute("CREATE TEMP TABLE pii_map (original text PRIMARY KEY, transformed text)")
    with open(MAPPING_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [(r["original_value"], r["transformed_value"]) for r in reader]
    from psycopg2.extras import execute_values
    execute_values(cur, "INSERT INTO pii_map (original, transformed) VALUES %s ON CONFLICT DO NOTHING", rows)
    conn.commit()
    print(f"Loaded {len(rows)} mapping rows in {time.time()-t0:.2f}s")

    print("\n=== Bucket 2: exact-value mapped fields ===")
    for table, column, fallback in EXACT_MAPPED_FIELDS:
        try:
            # mapped rows: replace via join
            cur.execute(
                f'UPDATE "{table}" t SET "{column}" = m.transformed '
                f'FROM pii_map m WHERE t."{column}" = m.original'
            )
            mapped_count = cur.rowcount
            # unmapped rows: still real data (not NULL/placeholder), no dictionary entry -- fallback
            cur.execute(
                f'UPDATE "{table}" SET "{column}" = %s '
                f'WHERE "{column}" IS NOT NULL AND "{column}" != %s '
                f'AND "{column}" NOT IN (SELECT transformed FROM pii_map)',
                (fallback, fallback),
            )
            fallback_count = cur.rowcount
            print(f"[{table}.{column}] {mapped_count} mapped, {fallback_count} fallback-placeholder")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"ERROR on {table}.{column}: {type(e).__name__}: {e}")

    print(f"\n=== Done in {time.time()-t0:.2f}s total ===")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
