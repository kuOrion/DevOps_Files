"""
Filter pass over the raw PII value dictionary dump.

Two outputs:
1. FULL transform set: every value from every genuinely identity-bearing
   (table, column) -- these get transformed at their own source field.
2. SUBSTRING-HUNT set: a stricter subset, safe to search-and-replace as a
   substring anywhere else in the database (full names, emails, addresses,
   VAT/bank numbers -- high-entropy, specific). Excludes short/common
   fragments (city/state/single words) that would cause false-positive
   collateral matches if used as a global search pattern.

Debug-by-default: prints every drop decision's reasoning, not just totals.
"""
import csv
import os
import re

from pii_registry import EXCLUDE_FIELDS, RES_COMPANY_ALLOWED

# Scoped by SOURCE_DB_NAME -- see build_pii_dictionary.py's OUTPUT_CSV
# comment for why an unscoped shared path is unsafe.
_DB_NAME = os.environ.get("SOURCE_DB_NAME", "orion_test")
INPUT_CSV = f"/tmp/pii_value_dictionary_{_DB_NAME}.csv"
OUTPUT_FULL = f"/tmp/pii_dictionary_full_{_DB_NAME}.csv"
OUTPUT_HUNT = f"/tmp/pii_dictionary_substring_hunt_{_DB_NAME}.csv"


def is_excluded(table, column):
    if (table, column) in EXCLUDE_FIELDS:
        return True
    if table == "res_company" and column not in RES_COMPANY_ALLOWED:
        return True
    return False


_TAG_RE = re.compile(r"<[^>]+>")


def qualifies_for_substring_hunt(value):
    """High-entropy / specific enough to safely search-and-replace as a
    substring anywhere in the DB without false-positive collateral damage
    on short/common fragments (city names, single words, etc).

    Strips HTML tags before evaluating length -- a bare markup fragment
    like '<p><br></p>' (11 raw characters, no space/digit/@) satisfied the
    old "long single word" fallback and got treated as a unique identifying
    string, when it's actually boilerplate that appears identically across
    every blank rich-text field in the database. Found via a live run that
    started corrupting HTML structure across crm_lead.description and
    others -- caught and killed before it completed.

    The "long single word" fallback itself was later dropped entirely --
    it separately caused a second, worse bug: the plain English word
    "Production" (a real hr.employee.job_title/res.partner.function value
    before job titles were flattened) qualified and matched inside
    unrelated system messages ("Production Order created"), corrupting
    them. Real names/emails/addresses/codes always have a space, @, or
    digit -- they never need this fallback -- so requiring one of those
    three signals is strictly safer with no loss of genuine coverage.
    """
    stripped = _TAG_RE.sub("", value).strip()
    if len(stripped) < 6:
        return False
    if len(value) < 6:
        return False
    has_space = " " in stripped
    has_at = "@" in stripped
    has_digit = any(c.isdigit() for c in stripped)
    long_enough_alnum = has_digit and len(stripped) >= 6  # vat/bank/id numbers
    return has_space or has_at or long_enough_alnum


def main():
    full_rows = []
    hunt_rows = []
    excluded_count = 0
    kept_count = 0
    hunt_count = 0

    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            table, column, value = row["table"], row["column"], row["value"]
            if is_excluded(table, column):
                excluded_count += 1
                continue
            if not value or not value.strip():
                continue
            kept_count += 1
            full_rows.append((table, column, value))
            if qualifies_for_substring_hunt(value):
                hunt_count += 1
                hunt_rows.append((table, column, value))

    print(f"Excluded (security/config/enum, not identity): {excluded_count}")
    print(f"Kept in FULL transform set: {kept_count}")
    print(f"Of those, qualify for SUBSTRING-HUNT set (high-entropy/specific): {hunt_count}")

    with open(OUTPUT_FULL, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "value"])
        w.writerows(full_rows)
    print(f"Wrote {OUTPUT_FULL}")

    with open(OUTPUT_HUNT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["table", "column", "value"])
        w.writerows(hunt_rows)
    print(f"Wrote {OUTPUT_HUNT}")

    # breakdown by (table, column) for review
    from collections import Counter
    full_by_field = Counter((t, c) for t, c, v in full_rows)
    print("\n=== Kept fields (table.column: count) ===")
    for (t, c), n in sorted(full_by_field.items()):
        print(f"  {t}.{c}: {n}")


if __name__ == "__main__":
    main()
