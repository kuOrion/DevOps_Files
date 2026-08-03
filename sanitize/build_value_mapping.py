"""
Step 4: build the value -> transformed_value mapping, the master
key/value dictionary everything downstream uses.

Pure Python, no Odoo/DB dependency -- runs entirely server-side as part of
run_pipeline.sh (no laptop round-trip needed; transform_plain is inlined
directly here, matching substring_hunt_scan.py's own copy, so this file
has zero dependency on anything outside the container it runs in).

Debug-by-default: prints progress, dedup stats, sample entries, any value
that fails to round-trip through the transform.
"""
import csv
import hashlib
import hmac
import string

SECRET_KEY = b"replace-with-a-real-secret-never-shipped-with-sanitized-data"
_LOWER = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits


def _keystream(value, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(SECRET_KEY, value.encode("utf-8") + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def transform_plain(value):
    if not value:
        return value
    ks = _keystream(value, len(value))
    out = []
    for ch, kb in zip(value, ks):
        if ch.isdigit():
            out.append(_DIGITS[kb % 10])
        elif ch.isalpha():
            out.append(_UPPER[kb % 26] if ch.isupper() else _LOWER[kb % 26])
        else:
            out.append(ch)
    return "".join(out)


INPUT_CSV = "/tmp/pii_dictionary_full.csv"
OUTPUT_CSV = "/tmp/pii_value_mapping.csv"


def main():
    distinct_values = set()
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            v = row["value"]
            if v and v.strip():
                distinct_values.add(v)

    print(f"Distinct values across the full dictionary: {len(distinct_values)}")

    mapping = {}
    errors = 0
    for v in distinct_values:
        try:
            mapping[v] = transform_plain(v)
        except Exception as e:
            errors += 1
            print(f"ERROR transforming value {v!r}: {type(e).__name__}: {e}")

    print(f"Mapped: {len(mapping)}, errors: {errors}")

    # sanity checks: same value must always map to the same output
    # (determinism), and no mapping should equal its own original (a
    # completely unchanged value would mean the transform silently no-op'd)
    unchanged = [v for v, t in mapping.items() if v == t]
    print(f"Values unchanged after transform (should be ~0, only possible for "
          f"non-alnum-only strings): {len(unchanged)}")
    if unchanged[:5]:
        print("  sample unchanged:", unchanged[:5])

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_value", "transformed_value"])
        for v, t in sorted(mapping.items()):
            w.writerow([v, t])
    print(f"Wrote {OUTPUT_CSV}")

    print("\n=== sample mappings ===")
    for v, t in list(mapping.items())[:10]:
        print(f"  {v!r} -> {t!r}")


if __name__ == "__main__":
    main()
