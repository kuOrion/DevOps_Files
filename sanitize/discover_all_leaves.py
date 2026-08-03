"""
Discovery-only census: how many real (compute=False, human-entered/imported)
char/text/html/integer/float/monetary fields exist across the whole business
schema, how many distinct (model, field) pairs that is, and how many actual
populated (non-null/non-empty/non-zero) values back them right now.

Excludes: transient models, SQL-view models (_auto=False), ir.*/base.*/
base_import.* (framework/technical), and mail.* (chatter/notes — already
handled separately and out of scope for this question).

Read-only. No writes, no commits. Run via:
  odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < discover_all_leaves.py
"""

EXCLUDED_MODEL_PREFIXES = ("ir.", "base.", "base_import.", "mail.")
TEXT_TYPES = ("char", "text", "html")
NUMERIC_TYPES = ("integer", "float", "monetary")


def discover(env):
    per_field = []  # (model, field, ftype, table, column)
    for model_name in env.registry.models:
        if ".tests." in model_name or model_name.startswith(EXCLUDED_MODEL_PREFIXES):
            continue
        try:
            Model = env[model_name]
        except Exception:
            continue
        if Model._transient or not Model._auto:
            continue
        table = Model._table
        for fname, f in Model._fields.items():
            if fname == "id":
                continue
            if f.type in TEXT_TYPES + NUMERIC_TYPES and f.store and not f.compute:
                per_field.append((model_name, fname, f.type, table))
    return per_field


def count_populated(env, per_field):
    results = []
    for model_name, fname, ftype, table in per_field:
        try:
            if ftype in TEXT_TYPES:
                env.cr.execute(
                    f'SELECT count(*) FROM "{table}" WHERE "{fname}" IS NOT NULL AND "{fname}" != \'\''
                )
            else:
                env.cr.execute(
                    f'SELECT count(*) FROM "{table}" WHERE "{fname}" IS NOT NULL AND "{fname}" != 0'
                )
            n = env.cr.fetchone()[0]
        except Exception:
            env.cr.rollback()
            n = None
        results.append((model_name, fname, ftype, table, n))
    return results


def main(env):
    per_field = discover(env)
    results = count_populated(env, per_field)

    by_type = {}
    total_populated = 0
    models_seen = set()
    for model_name, fname, ftype, table, n in results:
        by_type.setdefault(ftype, []).append((model_name, fname, n))
        models_seen.add(model_name)
        if n:
            total_populated += n

    print("=== CENSUS: compute=False text/numeric leaf fields (excl. mail/ir/base) ===")
    print(f"Distinct models touched: {len(models_seen)}")
    print(f"Distinct (model, field) pairs: {len(results)}")
    for ftype in TEXT_TYPES + NUMERIC_TYPES:
        fields = by_type.get(ftype, [])
        nonzero_fields = [x for x in fields if x[2]]
        print(f"  {ftype}: {len(fields)} fields, {len(nonzero_fields)} with at least 1 populated value")
    print(f"Total populated (non-null/non-empty/non-zero) cell count across all: {total_populated}")

    print()
    print("=== Top 40 fields by populated value count ===")
    ranked = sorted([r for r in results if r[4]], key=lambda r: -r[4])
    for model_name, fname, ftype, table, n in ranked[:40]:
        print(f"  {n:>10}  {model_name}.{fname}  ({ftype})")

    print()
    print("=== Fields with zero populated rows currently (candidates to ignore) ===")
    zero = [r for r in results if not r[4]]
    print(f"  {len(zero)} fields")

    return results


if __name__ == "__main__":
    main(env)  # noqa: F821
