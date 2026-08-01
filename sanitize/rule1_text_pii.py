"""
Rule 1: char/text/html sanitization via keyed format-preserving transform.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule1_text_pii.py

Design (see ROADMAP.md for the full reasoning): default to transforming
EVERY business-model char/text/html leaf field's value, and rely on a small,
purely mechanical set of per-value safety checks to skip what's unsafe —
no PII-detection, no name/label lists, no per-model category guessing.

Skipped, mechanically:
  - Transient models (_transient) and SQL-view-backed reporting models
    (_auto=False) — real Odoo API flags, not a guess.
  - Odoo's own framework/technical models (ir.*, base.*, base_import.*) —
    structural, not content-based.
  - Any value that IS a real Odoo model name (checked against ir.model) or
    a real XML-ID (checked against ir.model.data's module+name pairs) —
    e.g. mail.followers.res_model holding 'hr.employee'.
  - Any value shaped like Python domain-filter/code syntax (starts with
    '[(', or contains 'self.env'/'object.env'/'.env[') — e.g.
    gamification.challenge.user_domain holding "[('karma', '>', 0)]".
    Found on genuine BUSINESS models, not just ir.*/base.* ones — proves
    the model-prefix exclusion alone isn't sufficient, content-shape is.

Everything else gets transformed via ORM write() (never raw SQL — see
Rule 2's reasoning: compute fields like display_name only correctly
re-derive from a real ORM-tracked write, and raw SQL wouldn't trigger it).
"""
import hashlib
import hmac
import re
import string

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"

LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGITS = string.digits

CODE_SHAPE = re.compile(r"^\[\(|self\.env|object\.env|\.env\[")
TAG_RE = re.compile(r"(<[^>]+>)")

EXCLUDED_MODEL_PREFIXES = ("ir.", "base.", "base_import.")


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
        """Real PostgreSQL UNIQUE constraint on this column — a structural signal
        that it's a short structured reference code (country/currency/language
        codes etc.), not free text. Found via res.country.code crashing with a
        UniqueViolation during real-scale testing — collision risk is only
        negligible for long strings, not fixed-width 2-3 char codes."""
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


def discover_business_leaves(env):
    """Every char/text/html leaf field on a real, persisted business model."""
    leaves = []
    for model_name in env.registry.models:
        if ".tests." in model_name or model_name.startswith(EXCLUDED_MODEL_PREFIXES):
            continue
        try:
            Model = env[model_name]
        except Exception:
            continue
        if Model._transient or not Model._auto:
            continue
        for fname, f in Model._fields.items():
            if f.type in ("char", "text", "html") and f.store and not f.compute:
                leaves.append((model_name, fname, f.type))
    return leaves


def apply_rule1(env, batch_log_every=500):
    """Transform every safe value across every discovered business leaf field."""
    checker = SafetyChecker(env)
    leaves = discover_business_leaves(env)
    total_written = 0
    total_skipped = 0
    total_errors = 0

    for model_name, fname, ftype in leaves:
        Model = env[model_name]
        table = Model._table
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
            if not checker.is_safe_to_transform(val, table=table, column=fname):
                total_skipped += 1
                continue
            new_val = transform_html(val) if ftype == "html" else transform_plain(val)
            try:
                rec.write({fname: new_val})
                total_written += 1
            except Exception:
                env.cr.rollback()
                total_errors += 1
            if total_written % batch_log_every == 0 and total_written:
                env.cr.commit()
                print(f"... {total_written} written so far ({model_name}.{fname})")

    env.cr.commit()
    print(f"Rule 1 done: {total_written} written, {total_skipped} skipped (safety check), {total_errors} errors")
    return total_written, total_skipped, total_errors


if __name__ == "__main__":
    apply_rule1(env)  # noqa: F821 (env injected by odoo shell)
