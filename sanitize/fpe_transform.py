#!/usr/bin/env python3
"""
Prototype: keyed, deterministic, format-preserving transform for char/text/html.

- Same input string -> same output string, every time (relationship-preserving).
- Only letters/digits change; everything else (spaces, punctuation, @, ., HTML tags/attrs) untouched.
- Keyed by the WHOLE original string (not per-character in isolation), so it isn't
  a classical monoalphabetic substitution cipher vulnerable to frequency analysis.
"""
import hashlib
import hmac
import re
import string

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"

LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGITS = string.digits


def _keystream(value: str, length: int) -> bytes:
    """Expand HMAC-SHA256(key, value) into `length` bytes via a counter-based KDF."""
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(SECRET_KEY, value.encode("utf-8") + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def transform_plain(value: str) -> str:
    """Format-preserving transform for a plain (non-HTML) string."""
    if not value:
        return value
    ks = _keystream(value, len(value))
    out = []
    for ch, kb in zip(value, ks):
        if ch.isdigit():
            out.append(DIGITS[kb % 10])
        elif ch.isalpha():
            # any Unicode letter (accented, non-Latin script, etc.), not just ASCII a-z/A-Z —
            # mapped into plain ASCII output regardless of source script.
            out.append(UPPER[kb % 26] if ch.isupper() else LOWER[kb % 26])
        else:
            out.append(ch)  # spaces, punctuation, @, ., non-letter unicode, etc. untouched
    return "".join(out)


_TAG_RE = re.compile(r"(<[^>]+>)")  # split on tags, keeping them as their own segments


def transform_html(value: str) -> str:
    """Format-preserving transform for HTML: tags/attributes untouched, only text nodes transformed."""
    if not value:
        return value
    parts = _TAG_RE.split(value)
    return "".join(
        part if part.startswith("<") and part.endswith(">") else transform_plain(part)
        for part in parts
    )


if __name__ == "__main__":
    samples = [
        ("plain", "OdooBot"),
        ("plain", "Domestic projects"),
        ("plain", "chinmay.joag@gmail.com"),
        ("plain", "chinmay"),
        ("plain", "chinmay"),  # repeat, to prove determinism
        ("html", "<p>Domestic projects</p>"),
        ("html", '<p>Welcome to the <a href="https://example.com/general">#general</a> channel.</p>'),
    ]
    for kind, s in samples:
        result = transform_html(s) if kind == "html" else transform_plain(s)
        print(f"{kind:6} {s!r:60} -> {result!r}")
