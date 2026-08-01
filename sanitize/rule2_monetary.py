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

from discover_monetary_leaves import discover_monetary_leaves  # noqa: F821 (loaded via odoo shell alongside this file)

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"


def _factor_for(anchor_model: str, anchor_id: int) -> float:
    """Deterministic 0.5-2.0 factor, keyed by the anchor record's identity."""
    digest = hmac.new(SECRET_KEY, f"{anchor_model}:{anchor_id}".encode(), hashlib.sha256).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0.0-1.0
    return round(0.5 + unit * 1.5, 6)


def _find_anchor_field(env, model_name: str, leaf_model_names: set) -> str:
    """Find a many2one field on this model that is the REAL reverse side of a
    declared one2many on the target model — i.e. an actual parent-document
    relationship, not just any many2one pointing at a model that happens to
    have money in it. (First version used "many2one -> leaf-bearing model"
    alone and got fooled by company_id: res.company has a monetary leaf field
    too, and company_id exists on nearly every model, so it drowned out the
    real anchor like sale.order.line's order_id every time. A real one2many/
    many2one pair, discoverable via Odoo's own field metadata, is precise.)"""
    Model = env[model_name]
    candidates = []
    for fname, f in Model._fields.items():
        if f.type != "many2one" or f.comodel_name not in leaf_model_names or f.comodel_name == model_name:
            continue
        Comodel = env[f.comodel_name]
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


def apply_rule2(env, batch_log_every=200):
    leaves, _derived = discover_monetary_leaves(env)
    leaf_model_names = set(m for m, f, ro in leaves)

    by_model = {}
    for model_name, fname, ro in leaves:
        by_model.setdefault(model_name, []).append(fname)

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
                rec.write(vals)
                total_written += 1
            except Exception:
                env.cr.rollback()
                total_errors += 1
            if total_written % batch_log_every == 0 and total_written:
                env.cr.commit()
                print(f"... {total_written} written so far ({model_name})")

    env.cr.commit()
    print(f"Rule 2 done: {total_written} written, {total_errors} errors")
    return total_written, total_errors


if __name__ == "__main__":
    apply_rule2(env)  # noqa: F821
