#!/usr/bin/env python3
"""Cross-language conformance differential runner.

Runs the Python, TypeScript and PHP conformance suites, captures their complete
emitted results (including null fields), and compares them case-by-case.

The only normalization applied is deterministic reordering of JSON object keys.
List order, numeric types, and string values are preserved exactly:
* missing field != field: null
* "1.0" != "1.00"
* 1 != "1"
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

CONFORMANCE_DIR = Path(__file__).resolve().parents[2] / "contracts" / "conformance"


def canonical_json(value: object) -> str:
    """Return a deterministic JSON string with sorted object keys."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def run_implementation(name: str, cmd: list[str], emit_path: Path) -> list[dict]:
    result = subprocess.run(cmd + ["--emit", str(emit_path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"{name} suite failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(emit_path.read_text())


def assert_paypal_parity(py: dict, ts: dict, php: dict) -> list[str]:
    """Explicit PayPal surcharge parity checks across all three emitted outputs."""
    errors: list[str] = []
    for label, actual in [("python", py), ("typescript", ts), ("php", php)]:
        if actual is None or actual.get("provider") != "paypal":
            continue

        components = actual.get("components") or []
        matched_rules = actual.get("matched_rules") or []

        if not components:
            errors.append(f"{label}: PayPal result has no components")
            continue

        if components[0].get("type") != "processing":
            errors.append(f"{label}: first PayPal component is not 'processing'")

        if len(components) > 1 and components[1].get("type") != "surcharge":
            errors.append(f"{label}: second PayPal component is not a separate 'surcharge'")

        for i, comp in enumerate(components):
            if comp.get("source_rule_id") != matched_rules[i].get("rule_id"):
                errors.append(f"{label}: component[{i}].source_rule_id does not match matched_rules[{i}].rule_id")
            if "rate_percentage" not in comp or "fixed_amount" not in comp:
                errors.append(f"{label}: component[{i}] missing rate_percentage or fixed_amount")

        processing_fee = Decimal(actual["processing_fee"]["value"])
        total = sum(Decimal(comp["amount"]) for comp in components)
        if processing_fee != total:
            errors.append(f"{label}: processing_fee {processing_fee} != sum of components {total}")

        data = actual.get("data") or {}
        expected_provenance_keys = [
            "provider",
            "schema_version",
            "market",
            "content_sha256",
            "source_urls",
            "source_updated_at",
            "data_ref",
        ]
        if list(data.keys()) != expected_provenance_keys:
            errors.append(f"{label}: data provenance keys are not in the expected order: {list(data.keys())}")

    return errors


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        py_path = tmp / "python.json"
        ts_path = tmp / "typescript.json"
        php_path = tmp / "php.json"

        py_results = run_implementation("Python", ["uv", "run", "python", "run_python.py"], py_path)
        ts_results = run_implementation("TypeScript", ["node", "run_typescript.mjs"], ts_path)
        php_results = run_implementation("PHP", ["php", "run_php.php"], php_path)

    manifest = json.loads((CONFORMANCE_DIR / "manifest.json").read_text())
    manifest_ids = set(manifest["cases"])
    # Normalize manifest IDs to case id strings (paths without extension)
    manifest_ids = {Path(p).stem for p in manifest_ids}

    by_id = {
        "python": {r["id"]: r for r in py_results},
        "typescript": {r["id"]: r for r in ts_results},
        "php": {r["id"]: r for r in php_results},
    }

    # Validate emitted ID sets
    errors: list[str] = []
    for name, results in [("python", py_results), ("typescript", ts_results), ("php", php_results)]:
        ids = [r["id"] for r in results]
        if len(ids) != len(set(ids)):
            duplicates = {i for i in ids if ids.count(i) > 1}
            errors.append(f"{name}: duplicate emitted case IDs: {sorted(duplicates)}")
        missing = manifest_ids - set(ids)
        extra = set(ids) - manifest_ids
        if missing:
            errors.append(f"{name}: missing emitted results for {sorted(missing)}")
        if extra:
            errors.append(f"{name}: unexpected emitted results for {sorted(extra)}")

    if errors:
        print("\nCase-ID set validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    mismatches: list[dict] = []
    parity_errors: list[str] = []
    for case_id in sorted(manifest_ids):
        py = by_id["python"][case_id]
        ts = by_id["typescript"][case_id]
        php = by_id["php"][case_id]

        py_actual = canonical_json({"actual": py.get("actual"), "error": py.get("error")})
        ts_actual = canonical_json({"actual": ts.get("actual"), "error": ts.get("error")})
        php_actual = canonical_json({"actual": php.get("actual"), "error": php.get("error")})

        if not (py_actual == ts_actual == php_actual):
            mismatches.append(
                {
                    "id": case_id,
                    "python": py,
                    "typescript": ts,
                    "php": php,
                }
            )

        parity_errors.extend(assert_paypal_parity(py.get("actual"), ts.get("actual"), php.get("actual")))

    if mismatches:
        print(f"\nCross-language mismatches: {len(mismatches)}", file=sys.stderr)
        for mismatch in mismatches:
            print(json.dumps(mismatch, indent=2, default=str, sort_keys=True), file=sys.stderr)
        return 1

    if parity_errors:
        print("\nPayPal surcharge parity errors:", file=sys.stderr)
        for error in parity_errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    print("\nAll cross-language differential checks passed.")
    print(f"case_count={len(manifest_ids)} mismatch_count=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
