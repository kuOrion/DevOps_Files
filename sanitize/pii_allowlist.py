"""
Single source of truth for what gets sanitized, post-redesign (2026-08-02).

Superseded the earlier "transform every char/text/html field, mechanically
exclude what's unsafe" design (see archive/rule1_text_pii_v1_discovery_based.py
and archive/rule2_monetary_v1_discovery_based.py) after a field census on real
orion_test data showed the old default swept up ~1,900 operational/reference
fields (stock moves, valuation layers, tax names, payroll category labels)
for every ~85 fields that were actual individual/company PII — and that
mismatch was the direct cause of repeated collateral-damage findings (tax
rate digits scrambling, payslip category labels changing). Locked with the
user via an interactive per-field markup (2026-08-02) — every field below
was reviewed by name, not discovered by type.

TRANSFORM_FIELDS: individual/company identity fields -> keyed
  format-preserving character substitution (rule1_text_pii.py).
SCALE_FIELDS: compensation amounts -> per-record scaling factor
  (rule2_monetary.py). Deliberately excludes sale/purchase/stock/invoice
  amounts, which stay real by design (they're business-operational data,
  not PII, per the same review).
"""

TRANSFORM_FIELDS = {
    "res.partner": [
        "name", "email", "phone", "mobile", "street", "street2", "city", "zip",
        "function", "website", "vat", "company_name", "ref", "comment",
        "signup_type", "partner_latitude", "partner_longitude", "additional_info",
    ],
    "hr.employee": [
        "emergency_contact", "emergency_phone", "place_of_birth",
        "spouse_complete_name", "identification_id", "passport_id", "sinid",
        "ssnid", "visa_no", "permit_no", "pin", "barcode", "vehicle",
        "study_field", "study_school", "km_home_work", "children", "notes",
        "additional_note", "departure_description",
    ],
    "hr.contract": ["notes"],
    "res.partner.bank": ["acc_holder_name", "acc_number"],
    "res.users": ["login"],
    "resource.resource": ["name"],
}

# acc_number is a char field (account number stored as a string), not
# numeric — it goes through the same character-substitution transform as
# vat/passport_id above (format-preserving: digits stay digits), not
# Rule 2's multiplicative scaling. Caught during the Rule 2 rewrite
# (2026-08-02) — originally miscategorized as "financial/scale" in the
# widget markup since it's compensation-adjacent, but the field type rules
# out scaling.
SCALE_FIELDS = {
    "hr.contract": [
        "wage", "hra", "other_allowance", "travel_allowance",
        "meal_allowance", "medical_allowance",
    ],
    "hr.employee": ["hourly_cost"],
    "res.partner": ["debit_limit"],
}


def assert_allowlist_valid(env, field_map: dict):
    """Fail loudly if any (model, field) in an allowlist no longer exists —
    a hardcoded list can't self-correct the way discovery-based scanning
    could, so a typo or an upstream module rename must error immediately
    rather than silently skip a field forever."""
    errors = []
    for model_name, fnames in field_map.items():
        try:
            Model = env[model_name]
        except Exception:
            errors.append(f"model not found: {model_name}")
            continue
        for fname in fnames:
            if fname not in Model._fields:
                errors.append(f"field not found: {model_name}.{fname}")
    if errors:
        raise AssertionError("Allowlist validation failed:\n" + "\n".join(errors))
