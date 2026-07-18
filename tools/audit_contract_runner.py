#!/usr/bin/env python3
"""Run the contract audit for the Python, PHP, and TypeScript implementations.

When PAYPAL_FEE_DATA and STRIPE_FEE_DATA are set, the runner loads each
implementation from those directories and compares the audit counters.  When
the variables are unset, the runner falls back to a tiny synthetic fixture so
the integration plumbing can still be exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from payment_fee import PaymentFeeEngine
from payment_fee.audit import audit_contract


def load_core(path: Path) -> dict:
    with open(path / "json" / "core-fees.json") as f:
        return json.load(f)


def build_documents(paypal_path: Path | None, stripe_path: Path | None) -> dict:
    documents: dict[str, dict] = {}
    if paypal_path:
        documents["paypal"] = {"core": load_core(paypal_path)}
    if stripe_path:
        documents["stripe"] = {"core": load_core(stripe_path)}
    return documents


def run_external(command: list[str], documents: dict) -> dict:
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


def synthetic_documents() -> dict:
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


def main() -> int:
    paypal_env = os.environ.get("PAYPAL_FEE_DATA")
    stripe_env = os.environ.get("STRIPE_FEE_DATA")

    if paypal_env or stripe_env:
        documents = build_documents(
            Path(paypal_env) if paypal_env else None,
            Path(stripe_env) if stripe_env else None,
        )
    else:
        documents = synthetic_documents()

    python_engine = PaymentFeeEngine.from_documents(
        paypal=documents.get("paypal"),
        stripe=documents.get("stripe"),
    )
    python_result = asdict(audit_contract(python_engine))

    php_result = run_external(["php", "tools/audit_contract_runner.php"], documents)
    ts_result = run_external(["node", "tools/audit_contract_runner.mjs"], documents)

    print(json.dumps({"python": python_result, "php": php_result, "typescript": ts_result}, indent=2))

    mismatches = compare_counters(python_result, php_result, ts_result)
    if mismatches:
        print("\nMismatched counters:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
        return 1

    print("\nAll contract audit counters match across Python, PHP, and TypeScript.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
