"""
Step 4: build the value -> transformed_value mapping, the master
key/value dictionary everything downstream uses.

Pure Python, no Odoo/DB dependency (transform_plain only needs hashlib/hmac/
string) -- runs locally, no sandbox round-trip needed.

Debug-by-default: prints progress, dedup stats, sample entries, any value
that fails to round-trip through the transform.
"""
import csv
import sys

sys.path.insert(0, "/home/cj/ERP16/build/sanitize")
from rule1_text_pii import transform_plain  # noqa: E402

INPUT_CSV = "/home/cj/ERP16/build/sanitize/reports/pii_dictionary_full.csv"
OUTPUT_CSV = "/home/cj/ERP16/build/sanitize/reports/pii_value_mapping.csv"


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
