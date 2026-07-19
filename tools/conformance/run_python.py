#!/usr/bin/env python3
"""Run Python implementation against the shared conformance suite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from payment_fee import PaymentFeeEngine
from payment_fee.errors import PaymentFeeError

CONFORMANCE_DIR = Path(__file__).resolve().parents[2] / "contracts" / "conformance"


def strip_nulls(value: object) -> object:
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def normalize(result: dict | None) -> dict | None:
    if result is None:
        return None
    return strip_nulls(result)


def run_case(case: dict) -> dict:
    provider_documents = case.get("provider_documents") or {}
    actual: dict | None = None
    actual_error: dict | None = None
    try:
        engine = PaymentFeeEngine.from_documents(
            paypal=provider_documents.get("paypal"),
            stripe=provider_documents.get("stripe"),
        )
        response = engine.quote(case["request"])
        actual = response.model_dump(mode="json", by_alias=False, exclude_none=False)
    except PaymentFeeError as exc:
        actual_error = {"code": exc.code, "message": str(exc), "details": exc.details}
    except Exception as exc:
        return {
            "id": case["id"],
            "status": "error",
            "message": f"Unexpected error: {exc}",
        }

    expected = case.get("expected_result")
    expected_error = case.get("expected_error")

    status = "ok"
    if normalize(actual) != normalize(expected):
        status = "mismatch"

    if status == "ok" and normalize(actual_error) != normalize(expected_error):
        status = "mismatch"

    return {
        "id": case["id"],
        "status": status,
        "actual": actual,
        "expected": expected,
        "actual_error": actual_error,
        "expected_error": expected_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Python conformance suite.")
    parser.add_argument("--emit", type=Path, help="Write per-case actual results to this JSON file.")
    args = parser.parse_args()

    manifest_path = CONFORMANCE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    failures: list[dict] = []
    emitted: list[dict] = []
    for case_path in manifest.get("cases", []):
        full_path = CONFORMANCE_DIR / case_path
        case = json.loads(full_path.read_text())
        result = run_case(case)
        print(f"{result['id']}: {result['status']}")
        emitted.append(
            {
                "id": result["id"],
                "status": result["status"],
                "actual": result["actual"],
                "error": result["actual_error"],
            }
        )
        if result["status"] != "ok":
            failures.append(
                {
                    "id": result["id"],
                    "status": result["status"],
                    "field": "result" if normalize(result["actual"]) != normalize(result["expected"]) else "error",
                    "actual": result["actual"],
                    "expected": result["expected"],
                }
            )

    if args.emit:
        args.emit.write_text(json.dumps(emitted, indent=2))

    if failures:
        print("\nFailures:", file=sys.stderr)
        for failure in failures:
            print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    print("\nAll conformance cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
