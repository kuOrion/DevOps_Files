-- Sanitization pilot, run against parus_instruments_scratch ONLY.
-- Covers 3 of the 4 agreed categories: structured PII, financial scaling,
-- free-text/tracking placeholders. ir_attachment handled separately.
--
-- SUPERSEDED (2026-08-02): section 2 (financial scaling) directly scales
-- account_move/account_move_line via raw SQL. Proven via direct empirical
-- test (see ROADMAP.md) that this is unsafe for any monetary field Odoo
-- tracks as a compute dependency (e.g. price_subtotal/amount_total) — an
-- ORM write() to a real dependency (price_unit) later would silently
-- revert these to values derived from the real, unscaled inputs. The
-- correct approach is ORM-mediated: scale only genuine leaf fields
-- (compute=False, discovered via discover_monetary_leaves.py) through a
-- real odoo shell write(), never raw SQL, and let Odoo's own dependency
-- graph derive everything else. Sections 1 (PII) and 3 (messages) are
-- still valid as-is — this note applies to section 2 only.
-- Kept for reference/history, not meant to be run as-is going forward.

BEGIN;

-- 1. res_partner: rewrite structured PII fields in place.
-- Preserves null-vs-not-null shape (a field that was empty stays empty),
-- keeps id/is_company/parent_id/company_id untouched so all FK references
-- and the hierarchy/roles still work exactly as before.
UPDATE res_partner SET
  name              = CASE WHEN name IS NOT NULL THEN 'Test Partner ' || id ELSE NULL END,
  display_name      = CASE WHEN display_name IS NOT NULL THEN 'Test Partner ' || id ELSE NULL END,
  company_name      = CASE WHEN company_name IS NOT NULL THEN 'Test Company ' || id ELSE NULL END,
  email             = CASE WHEN email IS NOT NULL THEN 'partner' || id || '@example.test' ELSE NULL END,
  email_normalized  = CASE WHEN email_normalized IS NOT NULL THEN 'partner' || id || '@example.test' ELSE NULL END,
  phone             = CASE WHEN phone IS NOT NULL THEN '+91-90000' || lpad(id::text,5,'0') ELSE NULL END,
  mobile            = CASE WHEN mobile IS NOT NULL THEN '+91-91000' || lpad(id::text,5,'0') ELSE NULL END,
  phone_sanitized   = CASE WHEN phone_sanitized IS NOT NULL THEN '9100' || lpad(id::text,7,'0') ELSE NULL END,
  street            = CASE WHEN street IS NOT NULL THEN id || ' Test Street' ELSE NULL END,
  street2           = CASE WHEN street2 IS NOT NULL THEN 'Test Area' ELSE NULL END,
  function          = CASE WHEN function IS NOT NULL THEN 'Test Role' ELSE NULL END,
  comment           = CASE WHEN comment IS NOT NULL THEN 'Test comment placeholder.' ELSE NULL END,
  website           = CASE WHEN website IS NOT NULL THEN 'https://example.test' ELSE NULL END,
  vat               = CASE WHEN vat IS NOT NULL THEN 'TESTVAT' || id ELSE NULL END,
  company_registry  = CASE WHEN company_registry IS NOT NULL THEN 'TESTREG' || id ELSE NULL END,
  signup_token      = NULL,   -- real auth tokens, never needed on a dev copy
  partner_gid       = NULL;

-- 2. account_move + account_move_line: consistent per-record scaling factor.
-- Same factor applied to a move's header totals AND every one of its lines,
-- so debit/credit/balance stay internally consistent (a line-sum check
-- against the header total still passes on the sanitized data).
CREATE TEMP TABLE move_scale AS
  SELECT id, (0.5 + (id % 10) / 10.0 * 1.5)::numeric(4,2) AS factor
  FROM account_move;

UPDATE account_move m SET
  amount_untaxed        = m.amount_untaxed * s.factor,
  amount_tax             = m.amount_tax * s.factor,
  amount_total           = m.amount_total * s.factor,
  amount_residual        = m.amount_residual * s.factor,
  amount_untaxed_signed  = m.amount_untaxed_signed * s.factor,
  amount_tax_signed      = m.amount_tax_signed * s.factor,
  amount_total_signed    = m.amount_total_signed * s.factor,
  amount_residual_signed = m.amount_residual_signed * s.factor
FROM move_scale s WHERE s.id = m.id;

UPDATE account_move_line l SET
  price_unit     = l.price_unit * s.factor,
  price_subtotal = l.price_subtotal * s.factor,
  price_total    = l.price_total * s.factor,
  debit          = l.debit * s.factor,
  credit         = l.credit * s.factor,
  balance        = l.balance * s.factor,
  amount_currency = l.amount_currency * s.factor
FROM move_scale s WHERE s.id = l.move_id;

-- 3. mail_message + mail_tracking_value: blanket placeholder.
-- Only touch rows that actually have chatter content (message_type
-- distinguishes real user notes/logs from Odoo's own internal notifications,
-- but for this pilot we blank all body/subject text uniformly).
UPDATE mail_message SET
  body    = CASE WHEN body IS NOT NULL AND body != '' THEN '<p>Test message placeholder.</p>' ELSE body END,
  subject = CASE WHEN subject IS NOT NULL THEN 'Test subject' ELSE NULL END,
  email_from = CASE WHEN email_from IS NOT NULL THEN 'test@example.test' ELSE NULL END,
  record_name = CASE WHEN record_name IS NOT NULL THEN 'Test record' ELSE NULL END;

UPDATE mail_tracking_value SET
  old_value_char = CASE WHEN old_value_char IS NOT NULL THEN 'old-test-value' ELSE NULL END,
  new_value_char = CASE WHEN new_value_char IS NOT NULL THEN 'new-test-value' ELSE NULL END,
  old_value_text = CASE WHEN old_value_text IS NOT NULL THEN 'old-test-value' ELSE NULL END,
  new_value_text = CASE WHEN new_value_text IS NOT NULL THEN 'new-test-value' ELSE NULL END;

COMMIT;
