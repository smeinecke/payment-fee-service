#!/usr/bin/env python3
"""Run the contract audit for the Python, PHP, and TypeScript implementations.

Modes:
  --data-mode real     (default) Use checked-out provider data repositories.
                       Requires PAYPAL_FEE_DATA and STRIPE_FEE_DATA, or
                       --paypal-data and --stripe-data.
  --data-mode fixtures Use the small synthetic fixtures embedded in this tool.
                       This is intended for focused unit tests, not release
                       audits, and is rejected in the default release mode.

The runner validates schemas, records the data revisions and content hashes,
compiles audit counters independently in Python, PHP and TypeScript, and
fails if any counter differs across languages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from payment_fee import PaymentFeeEngine
from payment_fee.audit import audit_contract
from payment_fee.errors import DatasetValidationError

REQUIRED_COUNTERS = [
    "paypal_calculable_rules_total",
    "paypal_calculable_rules_parsed",
    "paypal_calculable_rules_skipped",
    "paypal_context_required",
    "stripe_calculable_rules_total",
    "stripe_calculable_rules_parsed",
    "stripe_calculable_rules_skipped",
    "stripe_context_required",
    "unknown_fields",
    "unknown_condition_dimensions",
    "unknown_condition_operators",
    "unsupported_fee_components",
    "unresolved_schedule_references",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-mode",
        choices=["real", "fixtures"],
        default=os.environ.get("AUDIT_DATA_MODE", "real"),
        help="Dataset source for the audit (default: real).",
    )
    parser.add_argument(
        "--paypal-data",
        default=os.environ.get("PAYPAL_FEE_DATA"),
        help="Path to the paypal-fee-data repository.",
    )
    parser.add_argument(
        "--stripe-data",
        default=os.environ.get("STRIPE_FEE_DATA"),
        help="Path to the stripe-fee-data repository.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path to write the JSON audit report.",
    )
    return parser.parse_args(argv)


def synthetic_documents() -> dict[str, Any]:
    return {
        "paypal": {
            "core": {
                "schema_version": 1,
                "countries": [
                    {
                        "country_code": "DE",
                        "derived": {
                            "status": "calculable",
                            "transaction_fee_rules": [
                                {
                                    "id": "other_commercial",
                                    "variant_id": "standard",
                                    "label": "Commercial transaction",
                                    "percentage": "2.49",
                                    "fixed_fee_schedule": "fixed__applies_to_markets=DE",
                                    "calculation_status": "calculable",
                                    "fee_components": [
                                        {"type": "percentage"},
                                        {"type": "fixed_fee_schedule"},
                                    ],
                                    "conditions": {},
                                }
                            ],
                            "fixed_fee_schedules": {"fixed__applies_to_markets=DE": {"entries": {"EUR": "0.35"}}},
                            "international_surcharge_schedules": {},
                            "maximum_fee_schedules": {},
                        },
                    }
                ],
            }
        },
        "stripe": {
            "core": {
                "schema_version": 1,
                "markets": [
                    {
                        "account_country": "US",
                        "rules": [
                            {
                                "rule_id": "stripe:US:card:base",
                                "provider": "stripe",
                                "account_country": "US",
                                "classification_status": "calculable_rule",
                                "behavior": "base",
                                "product_id": "payment",
                                "variant_id": "card",
                                "payment_method": "card",
                                "label": "Card payment",
                                "unit": "per_transaction",
                                "fee_components": [
                                    {"type": "percentage", "value": "2.9"},
                                    {"type": "fixed_amount", "amount": "0.30", "currency": "USD"},
                                ],
                            }
                        ],
                    }
                ],
            }
        },
    }


def load_documents(paypal_data: Path | None, stripe_data: Path | None) -> dict[str, Any]:
    documents: dict[str, Any] = {}
    if paypal_data:
        documents["paypal"] = {
            "core": json.loads((paypal_data / "json" / "core-fees.json").read_text()),
            "index": json.loads((paypal_data / "json" / "index.json").read_text()),
            "schemas": {
                "core": json.loads((paypal_data / "schemas" / "core-fees-v1.schema.json").read_text()),
                "index": json.loads((paypal_data / "schemas" / "index-v1.schema.json").read_text()),
            },
        }
    if stripe_data:
        documents["stripe"] = {
            "core": json.loads((stripe_data / "json" / "core-fees.json").read_text()),
            "index": json.loads((stripe_data / "json" / "index.json").read_text()),
            "schemas": {
                "core": json.loads((stripe_data / "schemas" / "core-fees-v1.schema.json").read_text()),
                "index": json.loads((stripe_data / "schemas" / "index-v1.schema.json").read_text()),
            },
        }
    return documents


def _load_optional_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text())
    return None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return None


def data_revision(data_path: Path) -> dict[str, Any]:
    crawler_revision = _load_optional_json(data_path / "meta" / "crawler-revision.json") or {}
    return {
        "data_commit": _git_commit(data_path) or crawler_revision.get("crawler_revision"),
        "crawler_revision": crawler_revision.get("crawler_revision"),
        "content_sha256": _file_sha256(data_path / "json" / "core-fees.json"),
        "generated_at": crawler_revision.get("generated_at"),
    }


def python_audit(documents: dict[str, Any]) -> tuple[dict[str, int], list[str], list[str]]:
    engine = PaymentFeeEngine.from_documents(
        paypal=documents.get("paypal"),
        stripe=documents.get("stripe"),
        validate=True,
    )
    result = audit_contract(engine)
    counters = {k: getattr(result, k) for k in REQUIRED_COUNTERS}
    providers = engine.providers()
    return counters, result.failures, providers


def run_external(command: list[str], documents: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        input=json.dumps(documents),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {proc.stderr}")
    return json.loads(proc.stdout)


def validate_counter(label: str, key: str, value: Any) -> list[str]:
    errors: list[str] = []
    if value is None:
        errors.append(f"{label}: counter {key!r} is null")
    elif not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label}: counter {key!r} is not an integer (got {type(value).__name__})")
    return errors


def validate_counters(label: str, counters: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_COUNTERS:
        if key not in counters:
            errors.append(f"{label}: missing required counter {key!r}")
        else:
            errors.extend(validate_counter(label, key, counters[key]))
    for key in counters:
        if key not in REQUIRED_COUNTERS:
            errors.append(f"{label}: unexpected counter key {key!r}")
    return errors


def acceptance_failures(label: str, counters: dict[str, int]) -> list[str]:
    failures: list[str] = []

    total = counters.get("paypal_calculable_rules_total", 0)
    parsed = counters.get("paypal_calculable_rules_parsed", 0)
    skipped = counters.get("paypal_calculable_rules_skipped", 0)
    context = counters.get("paypal_context_required", 0)
    if parsed + skipped != total:
        failures.append(
            f"{label}: PayPal counters do not reconcile: {parsed} parsed + {skipped} skipped != {total} total"
        )
    if not (0 <= context <= total):
        failures.append(f"{label}: PayPal context_required {context} is outside [0, {total}]")

    total = counters.get("stripe_calculable_rules_total", 0)
    parsed = counters.get("stripe_calculable_rules_parsed", 0)
    skipped = counters.get("stripe_calculable_rules_skipped", 0)
    context = counters.get("stripe_context_required", 0)
    if parsed + skipped != total:
        failures.append(
            f"{label}: Stripe counters do not reconcile: {parsed} parsed + {skipped} skipped != {total} total"
        )
    if not (0 <= context <= total):
        failures.append(f"{label}: Stripe context_required {context} is outside [0, {total}]")

    zero_keys = [
        "paypal_calculable_rules_skipped",
        "stripe_calculable_rules_skipped",
        "unknown_fields",
        "unknown_condition_dimensions",
        "unknown_condition_operators",
        "unsupported_fee_components",
        "unresolved_schedule_references",
    ]
    for key in zero_keys:
        if counters.get(key) != 0:
            failures.append(f"{label}: {key} must be 0, got {counters.get(key)}")

    return failures


def compare_counters(python: dict, php: dict, ts: dict) -> list[str]:
    mismatches: list[str] = []
    for key in REQUIRED_COUNTERS:
        values = {"python": python.get(key), "php": php.get(key), "typescript": ts.get(key)}
        if len({values[k] for k in values}) != 1:
            mismatches.append(f"{key}: {values}")
    return mismatches


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.data_mode == "real":
        if not args.paypal_data or not args.stripe_data:
            print(
                "error: real data mode requires PAYPAL_FEE_DATA and STRIPE_FEE_DATA "
                "environment variables or --paypal-data and --stripe-data flags.",
                file=sys.stderr,
            )
            return 1
        paypal_path = Path(args.paypal_data)
        stripe_path = Path(args.stripe_data)
        if not (paypal_path / "json" / "core-fees.json").exists():
            print(f"error: PayPal data repository not found at {paypal_path}", file=sys.stderr)
            return 1
        if not (stripe_path / "json" / "core-fees.json").exists():
            print(f"error: Stripe data repository not found at {stripe_path}", file=sys.stderr)
            return 1
        documents = load_documents(paypal_path, stripe_path)
        revisions = {
            "paypal": data_revision(paypal_path),
            "stripe": data_revision(stripe_path),
        }
    else:
        documents = synthetic_documents()
        revisions = {
            "paypal": {"source": "synthetic-fixture"},
            "stripe": {"source": "synthetic-fixture"},
        }

    try:
        python_counters, python_failures, providers = python_audit(documents)
    except DatasetValidationError as exc:
        print(f"error: dataset validation failed: {exc}", file=sys.stderr)
        return 1

    php_result = run_external(["php", "tools/audit_contract_runner.php"], documents)
    ts_result = run_external(["node", "tools/audit_contract_runner.mjs"], documents)

    php_counters = php_result.get("counters", {})
    ts_counters = ts_result.get("counters", {})
    php_failures = php_result.get("failures", [])
    ts_failures = ts_result.get("failures", [])

    validation_errors: list[str] = []
    validation_errors.extend(validate_counters("python", python_counters))
    validation_errors.extend(validate_counters("php", php_counters))
    validation_errors.extend(validate_counters("typescript", ts_counters))

    mismatches = compare_counters(python_counters, php_counters, ts_counters)

    acceptance: list[str] = []
    acceptance.extend(acceptance_failures("python", python_counters))
    acceptance.extend(acceptance_failures("php", php_counters))
    acceptance.extend(acceptance_failures("typescript", ts_counters))

    service_commit = _git_commit(Path(__file__).resolve().parents[1]) or "unknown"

    report = {
        "service_commit": service_commit,
        "providers": sorted(providers),
        "data_revisions": revisions,
        "counters": {
            "python": python_counters,
            "php": php_counters,
            "typescript": ts_counters,
        },
        "failures": {
            "python": python_failures,
            "php": php_failures,
            "typescript": ts_failures,
        },
        "validation_errors": validation_errors,
        "cross_language_mismatches": mismatches,
        "acceptance_failures": acceptance,
    }

    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True))

    print(json.dumps(report, indent=2, sort_keys=True))

    if validation_errors:
        print("\nCounter validation errors:", file=sys.stderr)
        for error in validation_errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    if mismatches:
        print("\nMismatched counters:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        return 1

    if acceptance:
        print("\nAcceptance failures:", file=sys.stderr)
        for failure in acceptance:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("\nAll contract audit counters match and acceptance criteria are satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
