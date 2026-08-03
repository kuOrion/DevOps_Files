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
import re

INPUT_CSV = "/tmp/pii_value_dictionary.csv"
OUTPUT_FULL = "/tmp/pii_dictionary_full.csv"
OUTPUT_HUNT = "/tmp/pii_dictionary_substring_hunt.csv"

# (table, column) pairs excluded entirely: security/credential fields (must
# NOT be touched -- mutating breaks auth) and pure config/state/enum fields
# with zero identity content.
EXCLUDE_FIELDS = {
    # security/credentials -- never touch
    ("res_users", "password"),
    ("res_users", "oauth_access_token"),
    ("res_users", "totp_secret"),
    ("res_partner", "signup_token"),
    ("res_users", "odoobot_state"),
    # config/enum/workflow-state, not identity
    ("res_partner", "type"),
    ("res_partner", "tz"),
    ("res_partner", "lang"),
    ("res_partner", "seo_name"),
    ("res_partner", "signup_type"),
    ("res_partner", "l10n_in_gst_treatment"),
    ("res_partner", "invoice_warn"),
    ("res_partner", "picking_warn"),
    ("res_partner", "purchase_warn"),
    ("res_partner", "sale_warn"),
    ("res_partner", "invoice_warn_msg"),
    ("res_partner", "picking_warn_msg"),
    ("res_partner", "purchase_warn_msg"),
    ("res_partner", "sale_warn_msg"),
    ("res_partner", "followup_status"),
    ("res_partner", "website_description"),
    ("res_partner", "website_meta_description"),
    ("res_partner", "website_meta_keywords"),
    ("res_partner", "website_meta_og_img"),
    ("res_partner", "website_meta_title"),
    ("res_partner", "website_short_description"),
    ("res_users", "notification_type"),
    ("hr_employee", "employee_type"),
    ("hr_employee", "gender"),
    ("hr_employee", "marital"),
    ("hr_employee", "certificate"),
    ("resource_resource", "resource_type"),
    ("resource_resource", "tz"),
    ("hr_contract", "kanban_state"),
    ("hr_contract", "schedule_pay"),
    ("hr_contract", "state"),
    ("hr_payslip", "state"),
    ("hr_payslip", "number"),  # sequence code like SLIP/001, not identifying
    # stock.warehouse: workflow-config enums, not identity
    ("stock_warehouse", "delivery_steps"),
    ("stock_warehouse", "manufacture_steps"),
    ("stock_warehouse", "reception_steps"),
    # res.country / res.country.state: formatting templates and labels, not identity
    ("res_country", "address_format"),
    ("res_country", "name_position"),
    ("res_country", "vat_label"),
    ("res_country_state", "l10n_in_tin"),  # India GST state-code reference, not identity
    # 2-letter country/state codes: low-entropy (26x26 space), scrambling
    # them causes frequent unique-constraint collisions (330 errors/run,
    # confirmed consistent) for essentially no privacy benefit -- knowing a
    # partner is associated with "MH" (Maharashtra) isn't identifying on its
    # own. Left real, not transformed, not hunted.
    ("res_country", "code"),
    ("res_country_state", "code"),
    # job title/designation -- flattened to a literal placeholder instead of
    # scrambled (write_pass.py's flatten_job_titles), same treatment for
    # hr.job.name/hr.employee.job_title/res.partner.function -- exclude from
    # the scramble pass AND the hunt-set entirely. Real titles are common
    # English phrases ("Project Manager", "Production") that cause
    # coincidental substring collisions elsewhere (e.g. helpdesk_ticket.name
    # containing "Project manager rights") once hunted for -- now moot since
    # they're flattened at the source instead of preserved-and-searched-for.
    ("hr_employee", "job_title"),
    ("res_partner", "function"),
    # res_company: drop the ~48 onboarding/state/config columns, keep only
    # genuinely identity-bearing ones (handled by ALLOW-list below instead
    # of enumerating every exclusion -- see res_company handling)
}

# res_company has ~57 columns, almost all onboarding-wizard state flags.
# Allowlist the genuinely identity-bearing ones instead of excluding each
# state column individually.
RES_COMPANY_ALLOWED = {
    "name", "email", "phone", "mobile", "company_details",
    "invoice_terms_html", "report_footer", "report_header",
}


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
