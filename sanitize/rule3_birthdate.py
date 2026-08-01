"""
Rule 3: birthdate shift via ORM write(), narrowed to birthdate-only fields.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule3_birthdate.py

Originally scoped to 4 personal-date fields (birthday, spouse_birthdate,
visa_expire, work_permit_expiration_date) found by label-matching across
all 1233 stored date/datetime fields in the registry. Narrowed further, per
explicit decision, to birthdate-only fields — visa/work-permit expiration
left untouched. Deliberately does NOT touch any operational date (invoice
dates, fiscal periods, payslip period dates, message timestamps) — those
are load-bearing for quarter/year-end logic, payroll calculations, and
message ordering, and were never in scope for any version of this rule.

Transform: keeps the year (useful for age-based testing logic), shifts
month+day deterministically via keyed HMAC — same approach as HIPAA's Safe
Harbor de-identification rule for dates. See build/sanitize/date_shift.py
for the transform itself and its leap-year edge-case testing.
"""
import hashlib
import hmac
import re
from datetime import date, timedelta

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"

BIRTHDATE_PATTERN = re.compile(r"birth", re.I)


def shift_date(d: date) -> date:
    if d is None:
        return d
    digest = hmac.new(SECRET_KEY, d.isoformat().encode("utf-8"), hashlib.sha256).digest()
    offset = int.from_bytes(digest[:2], "big") % 365

    jan1 = date(d.year, 1, 1)
    is_leap = (d.year % 4 == 0 and d.year % 100 != 0) or (d.year % 400 == 0)
    year_len = 366 if is_leap else 365
    day_of_year = (d - jan1).days

    new_day_of_year = (day_of_year + offset) % year_len
    return jan1 + timedelta(days=new_day_of_year)


def discover_birthdate_leaves(env):
    leaves = []
    for model_name in env.registry.models:
        if ".tests." in model_name:
            continue
        try:
            Model = env[model_name]
        except Exception:
            continue
        if Model._transient or not Model._auto:
            continue
        for fname, f in Model._fields.items():
            if f.type == "date" and f.store and not f.compute:
                if BIRTHDATE_PATTERN.search(fname) or BIRTHDATE_PATTERN.search(str(f.string or "")):
                    leaves.append((model_name, fname))
    return sorted(set(leaves))


def apply_rule3(env):
    leaves = discover_birthdate_leaves(env)
    print(f"Birthdate-only leaf fields found: {leaves}")
    total_written = 0
    total_errors = 0

    for model_name, fname in leaves:
        Model = env[model_name]
        try:
            recs = Model.search([(fname, "!=", False)])
        except Exception:
            env.cr.rollback()
            continue
        for rec in recs:
            try:
                val = getattr(rec, fname)
                new_val = shift_date(val)
                rec.write({fname: new_val})
                total_written += 1
            except Exception as e:
                env.cr.rollback()
                total_errors += 1
                print(f"ERROR on {model_name}({rec.id}): {type(e).__name__}: {e}")

    env.cr.commit()
    print(f"Rule 3 done: {total_written} written, {total_errors} errors")
    return total_written, total_errors


if __name__ == "__main__":
    apply_rule3(env)  # noqa: F821
