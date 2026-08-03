"""
Rule 2 (v2, allowlist-driven): scale only compensation amounts in
pii_allowlist.SCALE_FIELDS, via a per-record scaling factor.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule2_monetary.py

Design change from v1 (see archive/rule2_monetary_v1_discovery_based.py):
v1 discovered every monetary/float leaf field in the whole registry and
scaled it, including sale/purchase/stock/invoice amounts — which meant
building anchor-field detection (real one2many/many2one parent-document
pairs) so a document's line items and totals stayed internally consistent.
v2 only scales compensation fields (wage, allowances, hourly_cost,
debit_limit) on models that are NOT line items of a larger transactional
document — hr.contract/hr.employee/res.partner are each scaled by their
own record identity, no anchor/parent lookup needed. Sale/purchase/stock
amounts are out of scope entirely and stay real (see pii_allowlist.py).

Kept from v1 (still needed regardless of scope):
- The reconciled-account.payment raw-SQL fallback isn't relevant here (no
  account.payment field is in SCALE_FIELDS), so it's dropped — nothing in
  this allowlist has a business-rule write guard we've hit in practice.
  If one ever appears, the ORM write() will raise and get logged as an
  error rather than silently succeed incorrectly.
- Immediate per-write commit — same transactional-bug fix as Rule 1.
"""
import hashlib
import hmac
import sys

sys.path.insert(0, "/tmp")  # odoo shell run via stdin doesn't add the script's own dir to sys.path
from pii_allowlist import SCALE_FIELDS, assert_allowlist_valid  # noqa: E402,F821

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"


def _factor_for(model_name: str, rec_id: int) -> float:
    """Deterministic 0.5-2.0 factor, keyed by the record's own identity.
    All fields on the same record share one factor (e.g. a contract's wage,
    hra, and travel_allowance all scale together), but different records
    get independent factors — no parent-document anchor needed since none
    of these models are line items of a larger financial document."""
    digest = hmac.new(SECRET_KEY, f"{model_name}:{rec_id}".encode(), hashlib.sha256).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0.0-1.0
    return round(0.5 + unit * 1.5, 6)


def apply_rule2(env, batch_log_every=200):
    assert_allowlist_valid(env, SCALE_FIELDS)
    total_written = 0
    total_errors = 0

    for model_name, fnames in SCALE_FIELDS.items():
        Model = env[model_name]
        domain = ["|"] * (len(fnames) - 1) + [(f, "!=", 0) for f in fnames]
        try:
            recs = Model.search(domain)
        except Exception:
            env.cr.rollback()
            continue
        for rec in recs:
            factor = _factor_for(model_name, rec.id)
            try:
                vals = {f: (getattr(rec, f, 0) or 0) * factor for f in fnames}
            except Exception as e:
                env.cr.rollback()
                total_errors += 1
                print(f"ERROR (computing values) on {model_name}({rec.id}): {type(e).__name__}: {e}")
                continue
            try:
                rec.write(vals)
                env.cr.commit()  # immediately — same fix as Rule 1: a later
                # record's rollback must never discard prior successes.
                total_written += 1
            except Exception as e:
                env.cr.rollback()
                total_errors += 1
                print(f"ERROR on {model_name}({rec.id}): {type(e).__name__}: {e}")
            if total_written % batch_log_every == 0 and total_written:
                print(f"... {total_written} written so far ({model_name})")

    env.cr.commit()
    print(f"Rule 2 (v2, allowlist) done: {total_written} written, {total_errors} errors")
    return total_written, total_errors


if __name__ == "__main__":
    apply_rule2(env)  # noqa: F821
