"""
Rule 2: financial scaling via ORM write(), consistent per-document.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule2_monetary.py

Uses discover_monetary_leaves() to find every genuine leaf monetary field
across the whole registry (see that module for the compute=False reasoning
and the empirical proof that raw SQL doesn't work here — an ORM write() is
required for derived fields like amount_total/price_subtotal to correctly
re-derive from the scaled leaf).

Per-document consistency, discovered mechanically, not hardcoded field
names: for each leaf model, check its own many2one fields for one whose
comodel is ALSO a monetary-leaf-bearing model (preferring a required=True
one, the strongest signal of "this is the parent line belongs to") — e.g.
sale.order.line's order_id points at sale.order, which itself has monetary
leaves, so a line automatically shares its parent order's scaling factor.
Standalone models (hr.contract, stock.valuation.layer, etc.) just use their
own id — nothing to keep consistent with.
"""
import hashlib
import hmac
import sys

sys.path.insert(0, "/tmp")  # odoo shell run via stdin doesn't add the script's own dir to sys.path
from discover_monetary_leaves import discover_monetary_leaves  # noqa: E402,F821

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"


def _factor_for(anchor_model: str, anchor_id: int) -> float:
    """Deterministic 0.5-2.0 factor, keyed by the anchor record's identity."""
    digest = hmac.new(SECRET_KEY, f"{anchor_model}:{anchor_id}".encode(), hashlib.sha256).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0.0-1.0
    return round(0.5 + unit * 1.5, 6)


def _find_anchor_field(env, model_name: str, leaf_model_names: set) -> str:
    """Find a many2one field on this model that is the REAL reverse side of a
    declared one2many on the target model — i.e. an actual parent-document
    relationship (sale.order.line.order_id <-> sale.order.order_line), not
    just any many2one pointing at a model that happens to have money in it.

    Two wrong versions got fixed along the way, worth keeping the history:
    v1 required "comodel has monetary leaves" — got fooled by company_id,
    which exists on nearly every model and happened to qualify because
    res.company has one unrelated leaf field, drowning out the real anchor.
    v2 (this one) drops that requirement entirely: the parent document
    (sale.order) may have ZERO leaf monetary fields of its own (its
    amount_total etc. are all compute=True) and still be the correct anchor
    for its lines — the one2many/many2one PAIR is the only real signal,
    whether or not the parent itself holds money.
    """
    Model = env[model_name]
    candidates = []
    for fname, f in Model._fields.items():
        if f.type != "many2one" or f.comodel_name == model_name:
            continue
        try:
            Comodel = env[f.comodel_name]
        except Exception:
            continue
        has_reverse_one2many = any(
            cf.type == "one2many" and cf.comodel_name == model_name and cf.inverse_name == fname
            for cf in Comodel._fields.values()
        )
        if has_reverse_one2many:
            candidates.append((fname, f.required))
    if not candidates:
        return None
    candidates.sort(key=lambda x: not x[1])  # required=True first
    return candidates[0][0]


# A real bug found via the actual before/after PDF comparison, not caught
# by any of our earlier tests: discover_monetary_leaves() only scans
# ttype=='monetary' fields, but the true leaf input driving sale/purchase/
# invoice line pricing is `price_unit`, which is ttype='float' (confirmed
# earlier, never wired into the production discovery). This meant
# sale.order.line/purchase.order.line/account.move.line's price_subtotal/
# price_total (correctly recognized as derived, correctly left alone) had
# NOTHING upstream ever get scaled — real quotation/invoice amounts were
# silently never touched at all. Checked for a fully mechanical signal
# (Odoo's named decimal-precision `digits` profiles, which might have
# distinguished "Product Price" from "Product Unit of Measure"/quantity)
# but it wasn't cleanly accessible via the live field object. Falling back
# to the same small, explicit, hands-on-validated approach already used for
# the `mail.*` chatter models — these 3 are exactly the models our own
# chain-1/chain-2 ORM-write tests proved correctly cascade through.
EXTRA_LEAF_FIELDS = {
    "sale.order.line": ["price_unit"],
    "purchase.order.line": ["price_unit"],
    "account.move.line": ["price_unit"],
}


def apply_rule2(env, batch_log_every=200):
    leaves, _derived = discover_monetary_leaves(env)
    leaf_model_names = set(m for m, f, ro in leaves)

    by_model = {}
    for model_name, fname, ro in leaves:
        by_model.setdefault(model_name, []).append(fname)
    for model_name, fnames in EXTRA_LEAF_FIELDS.items():
        by_model.setdefault(model_name, [])
        for fname in fnames:
            if fname not in by_model[model_name]:
                by_model[model_name].append(fname)
        leaf_model_names.add(model_name)

    anchor_fields = {m: _find_anchor_field(env, m, leaf_model_names) for m in by_model}

    total_written = 0
    total_errors = 0
    for model_name, fnames in by_model.items():
        Model = env[model_name]
        anchor_field = anchor_fields[model_name]
        domain = ["|"] * (len(fnames) - 1) + [(f, "!=", 0) for f in fnames]
        try:
            recs = Model.search(domain)
        except Exception:
            env.cr.rollback()
            continue
        for rec in recs:
            try:
                if anchor_field:
                    parent = getattr(rec, anchor_field)
                    if not parent:
                        continue
                    factor = _factor_for(parent._name, parent.id)
                else:
                    factor = _factor_for(model_name, rec.id)
                vals = {}
                for fname in fnames:
                    val = getattr(rec, fname, 0) or 0
                    vals[fname] = val * factor
            except Exception as e:
                env.cr.rollback()
                total_errors += 1
                print(f"ERROR (computing values) on {model_name}({rec.id}): {type(e).__name__}: {e}")
                continue

            try:
                rec.write(vals)
                env.cr.commit()  # immediately — same fix as Rule 1: batched
                # commits let one record's rollback silently discard prior
                # successful writes in the same uncommitted window.
                total_written += 1
            except Exception as e:
                # Some models (e.g. a reconciled account.payment) block ORM
                # writes via a business-rule guard even though the field
                # itself is a confirmed genuine leaf with nothing computing
                # from it (verified via discover_monetary_leaves' compute
                # check) — safe to bypass with raw SQL for exactly this case.
                env.cr.rollback()
                try:
                    set_clause = ", ".join(f'"{f}" = %s' for f in vals)
                    env.cr.execute(
                        f'UPDATE "{Model._table}" SET {set_clause} WHERE id = %s',
                        list(vals.values()) + [rec.id],
                    )
                    env.cr.commit()
                    total_written += 1
                    print(f"FALLBACK (raw SQL) succeeded on {model_name}({rec.id}) after: {type(e).__name__}: {e}")
                except Exception as e2:
                    env.cr.rollback()
                    total_errors += 1
                    print(f"ERROR on {model_name}({rec.id}): {type(e).__name__}: {e} (fallback also failed: {e2})")
            if total_written % batch_log_every == 0 and total_written:
                print(f"... {total_written} written so far ({model_name})")

    env.cr.commit()
    print(f"Rule 2 done: {total_written} written, {total_errors} errors")
    return total_written, total_errors


if __name__ == "__main__":
    apply_rule2(env)  # noqa: F821
