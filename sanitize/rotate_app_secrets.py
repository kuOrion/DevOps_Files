"""
Step 7 (final): rotate/blank third-party and app-level secrets that live
inside application data itself, not in odoo.conf/SSM -- these were
excluded from substring_hunt_scan.py's scope entirely (it skips every
`ir_*` table as "framework/technical, not business data"), which is right
for most of `ir_*` but wrong for these two specific values, both flagged
as a known gap on 2026-08-01 and never actually implemented until now:

- `ir_config_parameter.database.secret` -- Odoo's own auto-generated
  signing key for password-reset/portal-share tokens. Left unrotated, a
  sanitized snapshot would carry the SAME live signing key as the source
  environment, letting anyone with the snapshot forge valid tokens against
  it. Rotated to a fresh random value, exactly like Odoo does on a real
  fresh install.
- `ir_mail_server.smtp_user`/`smtp_pass` -- SMTP credentials for outbound
  mail. `smtp_user` was a real company email address even though
  `smtp_pass` happened to be empty on this dataset; blanked regardless so
  a future run with a real password configured is covered too.

Bulk SQL, no ORM -- these are plain config rows, no compute dependents.

Debug-by-default: prints old/new values (secret itself is never printed).
"""
import secrets as pysecrets

import psycopg2

DB_HOST = "db_orion_test"
DB_NAME = "orm_test"
DB_USER = "odoo"
DB_PASSWORD = "F0aclHkVKiTxFwCHsf6UoS26"

SMTP_USER_PLACEHOLDER = "smtp-user@example.test"


def main():
    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    cur = conn.cursor()

    new_secret = pysecrets.token_hex(32)
    cur.execute(
        "UPDATE ir_config_parameter SET value = %s WHERE key = 'database.secret'",
        (new_secret,),
    )
    print(f"database.secret: rotated ({cur.rowcount} row updated, new value not printed)")
    conn.commit()

    cur.execute(
        "UPDATE ir_mail_server SET smtp_user = %s, smtp_pass = NULL WHERE smtp_user IS NOT NULL",
        (SMTP_USER_PLACEHOLDER,),
    )
    print(f"ir_mail_server.smtp_user/smtp_pass: {cur.rowcount} row(s) blanked to placeholder")
    conn.commit()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
