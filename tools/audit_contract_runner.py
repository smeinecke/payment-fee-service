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
from dataclasses import asdict
from pathlib import Path
from typing import Any

from payment_fee import PaymentFeeEngine
from payment_fee.audit import audit_contract
from payment_fee.errors import DatasetValidationError


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
            "payment_methods": _load_optional_json(stripe_data / "json" / "payment-methods.json"),
            "schemas": {
                "core": json.loads((stripe_data / "schemas" / "core-fees-v1.schema.json").read_text()),
                "index": json.loads((stripe_data / "schemas" / "index-v1.schema.json").read_text()),
                "payment_methods": _load_optional_json(stripe_data / "schemas" / "payment-methods-v1.schema.json"),
            },
        }
    return documents


def _load_optional_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text())
    return None


def data_revision(data_path: Path) -> dict[str, Any]:
    crawler_revision = _load_optional_json(data_path / "meta" / "crawler-revision.json") or {}
    return {
        "crawler_revision": crawler_revision.get("crawler_revision"),
        "content_sha256": _file_sha256(data_path / "json" / "core-fees.json"),
        "generated_at": crawler_revision.get("generated_at"),
    }


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def python_audit(documents: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    engine = PaymentFeeEngine.from_documents(
        paypal=documents.get("paypal"),
        stripe=documents.get("stripe"),
        validate=True,
    )
    return asdict(audit_contract(engine)), engine.providers()


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


def compare_counters(python: dict, php: dict, ts: dict) -> list[str]:
    mismatches = []
    all_keys = set(python) | set(php) | set(ts)
    for key in sorted(all_keys):
        values = {"python": python.get(key), "php": php.get(key), "typescript": ts.get(key)}
        normalized = [v for v in values.values() if v is not None]
        if normalized and any(v != normalized[0] for v in normalized):
            mismatches.append(f"{key}: {values}")
    return mismatches


def acceptance_failures(counters: dict[str, Any]) -> list[str]:
    failures = []
    required_zero = [
        "paypal_calculable_rules_skipped",
        "stripe_calculable_rules_skipped",
        "unknown_fields",
        "unknown_condition_operators",
        "unresolved_schedule_references",
    ]
    for key in required_zero:
        if counters.get(key) not in (0, None):
            failures.append(f"{key} must be 0, got {counters.get(key)}")
    return failures


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
        python_result, providers = python_audit(documents)
    except DatasetValidationError as exc:
        print(f"error: dataset validation failed: {exc}", file=sys.stderr)
        return 1

    php_result = run_external(["php", "tools/audit_contract_runner.php"], documents)
    ts_result = run_external(["node", "tools/audit_contract_runner.mjs"], documents)

    merged = {**python_result, **php_result, **ts_result}
    mismatches = compare_counters(python_result, php_result, ts_result)
    failures = acceptance_failures(merged)

    report = {
        "providers": sorted(providers),
        "data_revisions": revisions,
        "counters": {
            "python": python_result,
            "php": php_result,
            "typescript": ts_result,
        },
        "cross_language_mismatches": mismatches,
        "acceptance_failures": failures,
    }

    if args.report:
        args.report.write_text(json.dumps(report, indent=2))

    print(json.dumps(report, indent=2))

    if mismatches:
        print("\nMismatched counters:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        return 1

    if failures:
        print("\nAcceptance failures:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("\nAll contract audit counters match and acceptance criteria are satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
