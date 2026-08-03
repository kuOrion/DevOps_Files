def main(env):
    jobs = [
        ("sale.report_saleorder", [3342], "/tmp/post_san/01_sale_order.pdf"),
        ("account.report_invoice", [1186], "/tmp/post_san/02_invoice.pdf"),
        ("account.report_payment_receipt", [2], "/tmp/post_san/03_payment_receipt.pdf"),
        ("mrp.report_mrporder", [1691], "/tmp/post_san/04_manufacturing_order.pdf"),
        ("hr_payroll_community.report_payslipdetails", [8], "/tmp/post_san/05_payslip_details.pdf"),
        ("purchase_request.report_purchase_request", [1], "/tmp/post_san/06_purchase_request.pdf"),
        ("hr_expense.report_expense_sheet", [1], "/tmp/post_san/07_expense_report.pdf"),
        ("repair.report_repairorder2", [1], "/tmp/post_san/08_repair_order.pdf"),
        ("mrp.report_bom_structure", [15], "/tmp/post_san/09_bom_structure.pdf"),
        ("account.report_original_vendor_bill", [1076], "/tmp/post_san/10_vendor_bill.pdf"),
    ]
    import os
    os.makedirs("/tmp/post_san", exist_ok=True)
    for report_name, ids, outfile in jobs:
        try:
            report = env["ir.actions.report"].search([("report_name", "=", report_name)], limit=1)
            if not report:
                print(f"ERROR: report action not found for {report_name}")
                continue
            pdf_content, _ = report._render_qweb_pdf(report_name, ids)
            with open(outfile, "wb") as f:
                f.write(pdf_content)
            print(f"OK: {report_name} -> {outfile} ({len(pdf_content)} bytes)")
        except Exception as e:
            print(f"ERROR rendering {report_name} (ids={ids}): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(env)
