"""
Rule 1 (v2, allowlist-driven): transform only the individual/company PII
fields in pii_allowlist.TRANSFORM_FIELDS, via the same keyed
format-preserving transform as v1.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule1_text_pii.py

Design change from v1 (see archive/rule1_text_pii_v1_discovery_based.py):
v1 defaulted to transforming every char/text/html leaf field in the whole
registry and relied on mechanical exclusions to skip what's unsafe. A real
field census on orion_test (2026-08-02) showed that default swept up ~790
operational/reference fields (tax names, payroll category labels, product
attribute values) for every field that was actual PII — and that's what
caused the tax-rate-digit-scrambling and payslip-category-label findings.
v2 instead starts from an explicit, human-reviewed allowlist (see
pii_allowlist.py) — nothing outside it is touched, so there's no discovery
step and no exclusion list to maintain.

Kept from v1 (still needed regardless of scope):
- SafetyChecker's mechanical checks (system identifiers via ir.model/
  ir.model.data, code-shaped values, unique-constrained columns) — these
  protect against real crashes/corruption on the allowlisted fields too.
- Immediate per-write commit — the transactional bug (a later record's
  rollback silently discarding prior uncommitted successes) applies no
  matter how small the field list is.
"""
import hashlib
import hmac
import re
import string
import sys

sys.path.insert(0, "/tmp")  # odoo shell run via stdin doesn't add the script's own dir to sys.path
from pii_allowlist import TRANSFORM_FIELDS, assert_allowlist_valid  # noqa: E402,F821

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"

LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGITS = string.digits

CODE_SHAPE = re.compile(r"^\[\(|self\.env|object\.env|\.env\[")
TAG_RE = re.compile(r"(<[^>]+>)")


def _keystream(value: str, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(SECRET_KEY, value.encode("utf-8") + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def transform_plain(value: str) -> str:
    if not value:
        return value
    ks = _keystream(value, len(value))
    out = []
    for ch, kb in zip(value, ks):
        if ch.isdigit():
            out.append(DIGITS[kb % 10])
        elif ch.isalpha():
            out.append(UPPER[kb % 26] if ch.isupper() else LOWER[kb % 26])
        else:
            out.append(ch)
    return "".join(out)


def transform_html(value: str) -> str:
    if not value:
        return value
    parts = TAG_RE.split(value)
    return "".join(
        part if part.startswith("<") and part.endswith(">") else transform_plain(part)
        for part in parts
    )


class SafetyChecker:
    """Caches ir.model / ir.model.data lookups and unique-constraint checks."""

    def __init__(self, env):
        self.env = env
        self._model_names = None
        self._xmlids = None
        self._unique_columns = None

    def _has_unique_constraint(self, table: str, column: str) -> bool:
        if self._unique_columns is None:
            self.env.cr.execute("""
                select tc.table_name, kcu.column_name
                from information_schema.table_constraints tc
                join information_schema.key_column_usage kcu
                  on kcu.constraint_name = tc.constraint_name
                where tc.constraint_type in ('UNIQUE', 'PRIMARY KEY')
            """)
            self._unique_columns = set(self.env.cr.fetchall())
        return (table, column) in self._unique_columns

    def _is_system_identifier(self, value: str) -> bool:
        if self._model_names is None:
            self._model_names = set(m.model for m in self.env["ir.model"].search([]))
        if value in self._model_names:
            return True
        if self._xmlids is None:
            self._xmlids = set((d.module, d.name) for d in self.env["ir.model.data"].search([]))
        if "." in value:
            mod, name = value.split(".", 1)
            if (mod, name) in self._xmlids:
                return True
        return False

    def is_safe_to_transform(self, value, table: str = None, column: str = None) -> bool:
        if not isinstance(value, str) or not value:
            return False
        if CODE_SHAPE.search(value):
            return False
        if self._is_system_identifier(value):
            return False
        if table and column and self._has_unique_constraint(table, column):
            return False
        return True

    def is_safe_to_transform_numeric(self, table: str = None, column: str = None) -> bool:
        """Numeric fields (int/float) skip the string-only checks above
        (code-shape, system-identifier don't apply to a number) but still
        respect a real unique constraint."""
        if table and column and self._has_unique_constraint(table, column):
            return False
        return True


def apply_rule1(env, batch_log_every=200):
    """Transform every safe value across the allowlisted PII fields only."""
    assert_allowlist_valid(env, TRANSFORM_FIELDS)
    checker = SafetyChecker(env)
    total_written = 0
    total_skipped = 0
    total_errors = 0

    for model_name, fnames in TRANSFORM_FIELDS.items():
        Model = env[model_name]
        table = Model._table
        for fname in fnames:
            ftype = Model._fields[fname].type
            try:
                recs = Model.search([(fname, "!=", False)])
            except Exception:
                env.cr.rollback()
                continue
            for rec in recs:
                try:
                    val = getattr(rec, fname, None)
                except Exception:
                    env.cr.rollback()
                    continue

                if ftype in ("integer", "float"):
                    # Not a string — transform_plain iterates characters, so
                    # go via the string representation and cast back, rather
                    # than the char/text/html string-safety checks (which
                    # don't apply to a number).
                    if not val or not checker.is_safe_to_transform_numeric(table=table, column=fname):
                        total_skipped += 1
                        continue
                    try:
                        cast = int if ftype == "integer" else float
                        new_val = cast(transform_plain(str(val)))
                    except (ValueError, TypeError):
                        total_skipped += 1
                        continue
                else:
                    if not checker.is_safe_to_transform(val, table=table, column=fname):
                        total_skipped += 1
                        continue
                    new_val = transform_html(val) if ftype == "html" else transform_plain(val)

                try:
                    # no_vat_validation: base_vat's check_vat() constraint
                    # rejects our scrambled res.partner.vat values (GSTIN's
                    # regex requires specific fixed characters at specific
                    # positions, not just "digit stays digit/letter stays
                    # letter"). Odoo's own comment for this context key:
                    # "API pushes from external platforms where you have no
                    # control over VAT numbers" — exactly this case. Harmless
                    # on every other field/model, so applied unconditionally
                    # rather than special-cased to just the vat field.
                    rec.with_context(no_vat_validation=True).write({fname: new_val})
                    env.cr.commit()  # immediately — a later rollback must never
                    # be able to discard this write (see ROADMAP.md for the
                    # transactional bug this fixed).
                    total_written += 1
                except Exception as e:
                    env.cr.rollback()
                    total_errors += 1
                    print(f"ERROR on {model_name}({rec.id}).{fname}: {type(e).__name__}: {e}")
                if total_written % batch_log_every == 0 and total_written:
                    print(f"... {total_written} written so far ({model_name}.{fname})")

    env.cr.commit()
    print(f"Rule 1 (v2, allowlist) done: {total_written} written, {total_skipped} skipped (safety check), {total_errors} errors")
    return total_written, total_skipped, total_errors


if __name__ == "__main__":
    apply_rule1(env)  # noqa: F821 (env injected by odoo shell)
