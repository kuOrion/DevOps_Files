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
import os
import secrets as pysecrets

import psycopg2

# Resolved from env vars set by run_pipeline.sh (client-parameterized) --
# fall back to the orion_test-specific literals for standalone runs.
DB_HOST = os.environ.get("DBHOST", "db_orion_test")
DB_NAME = os.environ.get("SANITIZED_DB_NAME", "orm_test")
DB_USER = "odoo"
DB_PASSWORD = os.environ.get("DBPASS", "F0aclHkVKiTxFwCHsf6UoS26")

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

    # from_filter holds the SAME real email independently of smtp_user --
    # found via a full ir_mail_server column review, missed by the original
    # fix which only touched smtp_user/smtp_pass.
    cur.execute(
        "UPDATE ir_mail_server SET from_filter = %s WHERE from_filter IS NOT NULL",
        (SMTP_USER_PLACEHOLDER,),
    )
    print(f"ir_mail_server.from_filter: {cur.rowcount} row(s) blanked to placeholder")
    conn.commit()

    # Found via a full ir_config_parameter review: a LIVE Google OAuth
    # client secret (GOCSPX-... -- Google's own real-credential prefix) and
    # its paired client id, plus a third copy of the same real company
    # email as mail.default.from. None of these were ever in scope for
    # substring_hunt_scan.py (blanket ir_* exclusion).
    cur.execute(
        "UPDATE ir_config_parameter SET value = %s WHERE key = 'google_gmail_client_secret' AND value IS NOT NULL AND value != ''",
        ("placeholder-client-secret",),
    )
    print(f"google_gmail_client_secret: {cur.rowcount} row(s) blanked")
    cur.execute(
        "UPDATE ir_config_parameter SET value = %s WHERE key = 'google_gmail_client_id' AND value IS NOT NULL AND value != ''",
        ("placeholder-client-id.apps.googleusercontent.com",),
    )
    print(f"google_gmail_client_id: {cur.rowcount} row(s) blanked")
    cur.execute(
        "UPDATE ir_config_parameter SET value = %s WHERE key = 'mail.default.from' AND value IS NOT NULL",
        (SMTP_USER_PLACEHOLDER,),
    )
    print(f"mail.default.from: {cur.rowcount} row(s) blanked to placeholder")
    conn.commit()

    # Dormant staging keys from the 2026-07-21 incident (_bd_*-equivalent
    # naming pattern _mb*/_mcb64/_se_*) -- confirmed empty/inert in
    # CLAUDE.md's incident record, deliberately left in place on production
    # at the time and "deferred to the redesign's fuller sweep." This
    # redesign is that sweep -- delete them outright rather than let
    # evidence-adjacent key names persist into every dev snapshot.
    cur.execute(
        "DELETE FROM ir_config_parameter WHERE key ~ '^_(mb[0-9]+|mcb[0-9]+|se_[0-9a-f]+)$'"
    )
    print(f"dormant staging keys (_mb*/_mcb*/_se_*): {cur.rowcount} row(s) deleted")
    conn.commit()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
