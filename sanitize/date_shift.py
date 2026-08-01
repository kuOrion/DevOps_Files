#!/usr/bin/env python3
"""
Rule 3: sensitive date shift.

Only for label-matched personal dates (birthday, spouse_birthdate,
visa_expire, work_permit_expiration_date — see discover_date_leaves.py).
Most dates/datetimes must NOT be touched — they're operationally load-bearing
(message ordering, due dates, workflow timestamps).

Keeps the YEAR (useful for age-based testing logic), deterministically shifts
month+day (the actually-identifying part combined with a name) — the same
approach HIPAA's Safe Harbor de-identification rule uses for dates.
"""
import hashlib
import hmac
from datetime import date, timedelta

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"


def shift_date(d: date) -> date:
    """Same year, deterministically shifted month/day."""
    if d is None:
        return d
    key_input = d.isoformat().encode("utf-8")
    digest = hmac.new(SECRET_KEY, key_input, hashlib.sha256).digest()
    offset = int.from_bytes(digest[:2], "big") % 365  # 0..364, avoids relying on leap-day range

    jan1 = date(d.year, 1, 1)
    is_leap = (d.year % 4 == 0 and d.year % 100 != 0) or (d.year % 400 == 0)
    year_len = 366 if is_leap else 365
    day_of_year = (d - jan1).days  # 0-indexed

    new_day_of_year = (day_of_year + offset) % year_len
    return jan1 + timedelta(days=new_day_of_year)


if __name__ == "__main__":
    samples = [
        "1990-01-14", "1984-09-09", "2001-05-10", "1998-09-19", "1957-07-27",
        "1968-08-12", "1965-04-22", "1984-07-30", "1979-07-25", "1963-12-18",
        "2001-12-06", "1981-04-24", "1967-07-26", "1969-08-25", "1999-10-26",
        "2000-04-30", "1990-02-18", "1967-01-20", "1989-01-10",
        "1988-12-26", "1974-11-01", "1989-06-23", "1971-03-22", "2005-04-17",
        "1993-07-13", "1988-09-27",
        # synthetic leap-year edge cases
        "2000-02-29", "1996-02-29", "2020-12-31", "2004-02-29", "1900-02-28",
    ]
    seen = {}
    year_mismatches = 0
    determinism_failures = 0
    for s in samples:
        d = date.fromisoformat(s)
        r1 = shift_date(d)
        r2 = shift_date(d)
        if r1 != r2:
            determinism_failures += 1
        if r1.year != d.year:
            year_mismatches += 1
        print(f"{s} -> {r1.isoformat()}  (year preserved: {r1.year == d.year})")

    print()
    print(f"Determinism failures: {determinism_failures}")
    print(f"Year preservation failures: {year_mismatches}")
