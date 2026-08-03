"""
Step 5: write transformed values to the true source fields, via ORM (not
raw SQL -- required so compute=True/store=True dependents like
hr.employee.name get properly recomputed rather than left stale).

Restricted to genuine atomic-leaf models -- NOT hr.payslip.name, which is a
composite/denormalized string handled by the step-6 substring-splice pass
instead.

Includes archived rows (active_test=False), immediate per-write commit
(the transactional bug fix from earlier this session), and the
no_vat_validation context flag for res.partner.vat.

Debug-by-default: prints per-model progress, every error, final summary.
"""
import csv
import sys

TABLE_TO_MODEL = {
    "res_partner": "res.partner",
    "hr_employee": "hr.employee",
    "hr_contract": "hr.contract",
    "res_users": "res.users",
    "res_partner_bank": "res.partner.bank",
    "resource_resource": "resource.resource",
    "res_company": "res.company",
    "stock_warehouse": "stock.warehouse",
    "res_country": "res.country",
    "res_country_state": "res.country.state",
    "res_bank": "res.bank",
    # hr_payslip deliberately excluded -- composite field, handled by
    # step 6 substring-hunt instead, not a whole-value write target.
}


def load_mapping():
    mapping = {}
    with open("/tmp/pii_value_mapping.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapping[row["original_value"]] = row["transformed_value"]
    return mapping


def load_targets():
    """(model, field) pairs to write to, derived from the dictionary's
    (table, column) pairs, excluding hr_payslip."""
    targets = set()
    with open("/tmp/pii_dictionary_full.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            table, column = row["table"], row["column"]
            if table not in TABLE_TO_MODEL:
                continue
            targets.add((TABLE_TO_MODEL[table], column))
    return sorted(targets)


def main(env):
    mapping = load_mapping()
    targets = load_targets()
    print(f"Loaded mapping: {len(mapping)} distinct values")
    print(f"Write targets: {len(targets)} (model, field) pairs")
    for m, f in targets:
        print(f"  {m}.{f}")

    total_written = 0
    total_skipped_no_mapping = 0
    total_errors = 0

    for model_name, fname in targets:
        Model = env[model_name].with_context(active_test=False, no_vat_validation=True)
        try:
            recs = Model.search([(fname, "!=", False)])
        except Exception as e:
            print(f"ERROR searching {model_name}.{fname}: {type(e).__name__}: {e}")
            continue

        written_this_field = 0
        for rec in recs:
            try:
                current_val = getattr(rec, fname, None)
            except Exception as e:
                total_errors += 1
                print(f"ERROR reading {model_name}({rec.id}).{fname}: {e}")
                continue
            if not current_val or not isinstance(current_val, str):
                continue
            new_val = mapping.get(current_val)
            if new_val is None:
                total_skipped_no_mapping += 1
                print(f"NO MAPPING for {model_name}({rec.id}).{fname} = {current_val!r} -- skipped")
                continue
            if new_val == current_val:
                continue  # no-op, e.g. single-char values
            try:
                rec.write({fname: new_val})
                env.cr.commit()  # immediate commit -- transactional bug fix
                total_written += 1
                written_this_field += 1
            except Exception as e:
                env.cr.rollback()
                total_errors += 1
                print(f"ERROR writing {model_name}({rec.id}).{fname}: {type(e).__name__}: {e}")

        print(f"[{model_name}.{fname}] {written_this_field} written")

    env.cr.commit()
    print(f"\n=== Write pass done: {total_written} written, "
          f"{total_skipped_no_mapping} skipped (no mapping), {total_errors} errors ===")

    flatten_job_titles(env)


def flatten_job_titles(env):
    """Job titles/designations get flattened to a single fixed value, not
    scrambled -- zero distinction between employees eliminates the
    re-identification risk from a rare/unique title entirely, cleaner than
    trying to judge which titles are 'safe' to leave real."""
    hr_job = env['hr.job'].with_context(active_test=False).search([])
    for j in hr_job:
        j.write({'name': 'Employee'})
    env.cr.commit()
    print(f"hr.job: {len(hr_job)} flattened to 'Employee'")

    employees = env['hr.employee'].with_context(active_test=False).search([('job_title', '!=', False)])
    for e in employees:
        e.write({'job_title': 'Employee'})
    env.cr.commit()
    print(f"hr.employee.job_title: {len(employees)} flattened to 'Employee'")

    # res.partner.function is an EXTERNAL contact's designation (customer/
    # vendor staff -- Purchase Manager, Director, etc, not Orion's own
    # employees), so it gets its own placeholder rather than "Employee" --
    # also flattens away several rows where a real person's name was typed
    # directly into this field by data-entry error (e.g. "mahesh bhogade",
    # "Dinesh Mishra- Director"), a leak class no scramble transform would
    # have reliably caught.
    partners = env['res.partner'].with_context(active_test=False).search([('function', '!=', False)])
    for p in partners:
        p.write({'function': 'Contact'})
    env.cr.commit()
    print(f"res.partner.function: {len(partners)} flattened to 'Contact'")


if __name__ == "__main__":
    main(env)
