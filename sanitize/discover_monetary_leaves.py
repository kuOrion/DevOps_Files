"""
Rule 2 (financial scaling) — mechanical discovery of scaling targets.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < discover_monetary_leaves.py

Finds every stored monetary field across the ENTIRE live registry (all installed
modules, not a hand-picked table list) and classifies each as:
  - LEAF (compute is falsy): a genuine stored value. Scale this directly, via
    ORM write() (never raw SQL — see ROADMAP.md's "why ORM, not SQL" note).
  - DERIVED (compute is truthy): do NOT touch this directly. Odoo's own
    @api.depends graph will correctly recompute it once its leaf dependencies
    are scaled, however deep or module-specific the chain is.

Excludes wizard models (_transient=True) and Odoo's own test-support models
(name contains '.tests.') — neither is real persisted business data.
"""

def discover_monetary_leaves(env):
    leaves = []
    derived = []
    for model_name in env.registry.models:
        if '.tests.' in model_name:
            continue
        try:
            Model = env[model_name]
        except Exception:
            continue
        if Model._transient:
            continue
        for fname, f in Model._fields.items():
            if f.type == 'monetary' and f.store:
                entry = (model_name, fname, f.readonly)
                (derived if f.compute else leaves).append(entry)
    return sorted(set(leaves)), sorted(set(derived))


if __name__ == '__main__':
    leaves, derived = discover_monetary_leaves(env)  # noqa: F821 (env is injected by odoo shell)
    print(f'Leaf (scale directly): {len(leaves)}')
    print(f'Derived (never touch, let Odoo recompute): {len(derived)}')
    for m, f, ro in leaves:
        print(f'LEAF {m}.{f} readonly={ro}')
