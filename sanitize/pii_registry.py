"""
Single shared source of truth for sanitization field classification.

Merges what were three separately-hardcoded dicts -- filter_pii_dictionary.py's
EXCLUDE_FIELDS, write_pass.py's TABLE_TO_MODEL, substring_hunt_scan.py's
KNOWN_DESTINATIONS/EXCLUDE_DESTINATIONS -- into one file every pipeline
script imports from, instead of each keeping its own copy that can drift.

This registry is deliberately schema-INTERSECTED at runtime, not assumed
complete: every script checks each entry against what actually exists in
the target database's schema (via information_schema.columns) before
using it. A registry entry for an Orion-custom-module table (e.g.
sh_access_manager) simply never matches on a client that doesn't have
that module installed -- no per-client branching needed in the code
itself, and no error either. Confirmed by tracing through
build_pii_dictionary.py (a table with 0 columns just yields 0 rows) and
substring_hunt_scan.py (columns to scan are discovered from the schema,
never assumed) before relying on this for a second client's data shape.

Growing this registry (not rewriting it, not branching per-client in
code) is the intended way new clients get supported: when a new client's
hunt-scan surfaces a NOVEL destination, it gets reviewed once and added
here permanently -- every future client benefits automatically. See
ROADMAP.md's TODO for the planned scanner that would surface candidate
NEW canonical-identity-shaped fields automatically, rather than relying
solely on the hunt-scan's novel-destination flagging.
"""

# ---------------------------------------------------------------------------
# CANONICAL_TABLES: which tables build_pii_dictionary.py dumps real values
# FROM, to build the value dictionary. A superset of TABLE_TO_MODEL below --
# hr_payslip is a dump-only source (its .name is a composite/denormalized
# string, handled by the hunt-scan splice pass, not a whole-value write
# target).
# ---------------------------------------------------------------------------
CANONICAL_TABLES = [
    "res_partner",
    "hr_employee",
    "hr_contract",
    "res_users",
    "res_partner_bank",
    "resource_resource",
    "res_company",
    "hr_payslip",  # denormalized name field found this session
    "stock_warehouse",  # real brand/company name found embedded here (PO shipping address)
    "res_country",  # geographic reference data, scrubbed alongside PII
    "res_country_state",
    "res_bank",  # bank institution name (distinct from res.partner.bank, the customer's account link)
]

# ---------------------------------------------------------------------------
# TABLE_TO_MODEL: which of CANONICAL_TABLES get whole-value ORM writes back
# (write_pass.py) -- hr_payslip deliberately excluded, see above.
# ---------------------------------------------------------------------------
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
}

# ---------------------------------------------------------------------------
# FLATTEN_FIELDS: (model, field) -> fixed literal replacement instead of
# scrambling. Job titles/designations are common phrases, not rare
# identifiers, and scrambling them created real false-positive collision
# risk in the hunt-set (see EXCLUDE_FIELDS' job-title entries below --
# "Project Manager"/"Production" both caused genuine corruption bugs
# before titles were excluded from scrambling entirely). Different literal
# for internal staff (hr.job/hr.employee) vs external business contacts
# (res.partner.function) since they're semantically different roles --
# also flattens away real person names occasionally typed directly into
# the designation field by data-entry error, a leak class no scramble
# transform would have reliably caught.
# ---------------------------------------------------------------------------
FLATTEN_FIELDS = {
    ("hr.job", "name"): "Employee",
    ("hr.employee", "job_title"): "Employee",
    ("res.partner", "function"): "Contact",
}

# ---------------------------------------------------------------------------
# EXCLUDE_FIELDS: (table, column) pairs excluded entirely from the FULL
# transform set -- security/credential fields (must never be touched by
# this pipeline) and pure config/enum/workflow-state fields with zero
# identity content. (hr.job.name/hr.employee.job_title/res.partner.function
# are ALSO excluded here, from the scramble pass specifically -- they're
# handled instead by FLATTEN_FIELDS above.)
# ---------------------------------------------------------------------------
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
    # job title/designation -- see FLATTEN_FIELDS above for the actual
    # treatment; excluded here from the scramble pass AND the hunt-set
    # entirely (real titles are common English phrases that caused
    # coincidental substring collisions once hunted for -- moot now that
    # they're flattened at the source instead of preserved-and-searched-for).
    ("hr_employee", "job_title"),
    ("res_partner", "function"),
    # res_company: drop the ~48 onboarding/state/config columns, keep only
    # genuinely identity-bearing ones (RES_COMPANY_ALLOWED below instead of
    # enumerating every exclusion individually).
}

# res_company has ~57 columns, almost all onboarding-wizard state flags.
# Allowlist the genuinely identity-bearing ones instead of excluding each
# state column individually.
RES_COMPANY_ALLOWED = {
    "name", "email", "phone", "mobile", "company_details",
    "invoice_terms_html", "report_footer", "report_header",
}

# ---------------------------------------------------------------------------
# KNOWN_DESTINATIONS: (table, column) pairs already reviewed and approved
# as legitimate denormalization-copy destinations for the substring-hunt
# scan to auto-fix. Grows over time as new clients surface new (but
# genuinely PII-bearing) destinations -- this is the registry's main
# growth point.
# ---------------------------------------------------------------------------
KNOWN_DESTINATIONS = {
    ("hr_payslip", "name"),
    ("account_move", "l10n_in_gstin"),
    ("account_move_line", "name"),
    ("calendar_event", "description"),
    ("calendar_event", "name"),
    ("crm_lead", "city"),
    ("crm_lead", "contact_name"),
    ("crm_lead", "description"),
    ("crm_lead", "function"),
    ("crm_lead", "mobile"),
    ("crm_lead", "name"),
    ("crm_lead", "partner_name"),
    ("crm_lead", "phone_sanitized"),
    ("crm_lead", "street"),
    ("crm_lead", "street2"),
    ("crm_lead", "website"),
    ("crm_lead", "zip"),
    ("crm_sales_visit", "discussion"),
    ("crm_sales_visit", "purpose"),
    ("document_page", "template"),
    ("document_page_history", "content"),
    ("helpdesk_ticket", "description"),
    ("hr_resume_line", "name"),
    ("mail_activity", "note"),
    ("mail_activity", "res_name"),
    ("mail_activity", "summary"),
    ("mail_mail", "body_html"),
    ("mail_mail", "email_to"),
    ("mail_message", "body"),
    ("mail_message", "email_from"),
    ("mail_message", "record_name"),
    ("mail_message", "reply_to"),
    ("mail_message", "subject"),
    ("mail_notification", "sms_number"),
    ("mail_tracking_value", "new_value_char"),
    ("mail_tracking_value", "old_value_char"),
    ("mailing_contact", "email"),
    ("mailing_contact", "email_normalized"),
    ("mailing_contact", "name"),
    ("mrp_production", "customer_name"),
    ("project_project", "description"),
    ("project_task", "description"),
    ("project_task", "name"),
    ("purchase_order", "note"),
    ("purchase_order_line", "specification"),
    ("res_bank", "account_number"),
    ("res_bank", "street"),
    ("res_company", "invoice_terms_html"),
    ("res_company", "report_footer"),
    ("res_partner", "mobile"),
    ("res_partner", "street"),
    ("res_partner", "street_name"),
    ("res_partner_title", "name"),
    ("sale_order", "customer_po_number"),
    ("sale_order", "origin"),
    ("sale_order", "your_reference"),
    ("sh_access_manager", "name"),
    ("sms_sms", "body"),
    ("sms_sms", "number"),
    ("stock_move", "origin"),
    ("stock_picking", "origin"),
    ("survey_user_input", "email"),
    ("survey_user_input", "nickname"),
    ("stock_route", "name"),  # auto-generated from warehouse name, e.g. "Orion Instruments, Pune: Cross-Dock"
}

# Reference/technical/category data that coincidentally matched a hunt
# pattern but is NOT personal or company identity -- permanently excluded
# so the scan stops re-flagging it every run. Reviewed once, remembered.
EXCLUDE_DESTINATIONS = {
    ("crm_activity_report", "body"),           # SQL view over mail.message-adjacent source, not updatable, source already fixed
    ("account_account", "code"),               # chart-of-accounts code
    ("account_analytic_account", "name"),      # found via parus_instruments: ALL rows create_uid=1 (Odoo's own shipped demo dataset -- "Deco Addict", "Asustek", "Nebula" etc, not real customers), coincidentally matched a real dictionary value
    ("account_payment_term", "note"),           # generic boilerplate
    ("helpdesk_ticket", "name"),                # coincidental job-title-phrase match ("Project manager rights"), not personal data -- moot now that job titles are excluded from the hunt-set
    ("hr_contract_type", "name"),               # category label (Consultant/Permanent/...)
    ("hr_department", "name"),                  # org-structure category label
    ("hr_department", "complete_name"),
    ("hr_job", "name"),                         # job-position category label
    ("l10n_in_port_code", "name"),              # public port reference list
    ("mail_channel", "name"),                   # system labels (OdooBot, Administrator)
    ("mail_channel_member", "custom_channel_name"),
    ("mail_message", "message_id"),             # auto-generated internal id
    ("mail_mail", "references"),                # auto-generated internal id
    ("mail_template", "body_html"),             # Odoo's own stock demo placeholder text
    ("mail_tracking_value", "field_desc"),      # field/role label, not an individual
    ("mrp_bom_line", "product_internal_reference"),  # product code
    ("product_attribute_value", "name"),        # product config option label
    ("product_attribute_value", "description"),
    ("product_product", "default_code"),        # product SKU code
    ("product_template", "default_code"),
    ("product_template", "name"),               # product model number, not identity
    ("product_template_attribute_value", "description"),
    ("report_project_task_user", "name"),       # SQL view over project_task, not updatable
    ("res_country", "name"),                    # static reference table
    ("res_country_state", "code"),
    ("res_country_state", "name"),
    ("res_groups", "comment"),                  # generic system documentation text
    ("res_groups", "name"),                      # system permission group name
    ("sale_order", "port_of_discharge"),        # shipping port reference
    ("sale_order_line", "name"),                # product code
    ("sh_product_template_attribute_value_line", "name"),
    ("sh_product_template_attribute_value_line", "description"),
    ("sh_product_variant_spec_line", "sh_value"),
    ("stock_location", "barcode"),              # internal warehouse code
    ("stock_lot", "name"),                      # lot/batch number
    # found via puna_eye_care (2026-08-05) -- the event_sale/survey/hr_holidays
    # modules bring Odoo's own stock demo dataset along (demo events, demo
    # survey questions, demo leave records), all create_uid=1, coincidentally
    # matching real dictionary values from puna_eye_care's actual data
    # elsewhere ("Ron Gibson", "Mitchell Admin", city names in demo survey
    # answers, etc.) -- same shape as account_analytic_account.name above,
    # not a real leak. CAVEAT (same one that entry already carries): this is
    # a blanket table+column exclusion, not conditioned on create_uid -- if
    # this client ever has REAL non-demo records in these fields (a real
    # event, a real employee's real leave request, a real survey response),
    # this exclusion would silently cover those too. Confirmed 100% of rows
    # were create_uid=1 at the time this was added; revisit if that changes.
    ("event_event", "name"),
    ("event_registration", "email"),
    ("event_registration", "name"),
    ("event_registration", "phone"),
    ("event_sale_report", "event_registration_name"),
    ("hr_leave_allocation", "private_name"),
    ("hr_leave_report", "name"),
    ("resource_calendar_leaves", "name"),
    ("survey_question", "title"),
    ("survey_question_answer", "value"),
    ("survey_user_input_line", "value_char_box"),
}

EXCLUDE_TABLES_PREFIX = ("ir_", "base_import_")  # framework/technical, not business data
