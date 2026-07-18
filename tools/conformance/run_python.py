#!/usr/bin/env python3
"""Run Python implementation against the shared conformance suite."""

from __future__ import annotations

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
    try:
        engine = PaymentFeeEngine.from_documents(
            paypal=provider_documents.get("paypal"),
            stripe=provider_documents.get("stripe"),
        )
        response = engine.quote(case["request"])
        actual = response.model_dump(mode="json", by_alias=False, exclude_none=True)
        actual_error = None
    except PaymentFeeError as exc:
        actual = None
        actual_error = {"code": exc.code, "message": str(exc), "details": exc.details}
    except Exception as exc:
        return {
            "id": case["id"],
            "status": "error",
            "message": f"Unexpected error: {exc}",
        }

    expected = case.get("expected_result")
    expected_error = case.get("expected_error")

    if normalize(actual) != normalize(expected):
        return {
            "id": case["id"],
            "status": "mismatch",
            "field": "result",
            "actual": actual,
            "expected": expected,
        }

    if normalize(actual_error) != normalize(expected_error):
        return {
            "id": case["id"],
            "status": "mismatch",
            "field": "error",
            "actual": actual_error,
            "expected": expected_error,
        }

    return {"id": case["id"], "status": "ok"}


def main() -> int:
    manifest_path = CONFORMANCE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    failures: list[dict] = []
    for case_path in manifest.get("cases", []):
        full_path = CONFORMANCE_DIR / case_path
        case = json.loads(full_path.read_text())
        result = run_case(case)
        if result["status"] != "ok":
            failures.append(result)
        print(f"{result['id']}: {result['status']}")

    if failures:
        print("\nFailures:", file=sys.stderr)
        for failure in failures:
            print(json.dumps(failure, indent=2), file=sys.stderr)
        return 1

    print("\nAll conformance cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
