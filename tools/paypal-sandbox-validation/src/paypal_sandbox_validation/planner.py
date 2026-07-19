from __future__ import annotations

import secrets
import uuid
from typing import Any

from paypal_sandbox_validation.configuration import (
    currency_for_country,
    resolve_amount_for_country,
    resolve_amounts,
)
from paypal_sandbox_validation.models import Case
from paypal_sandbox_validation.quote_adapter import QuoteAdapter


def generate_run_id() -> str:
    return uuid.uuid4().hex[:16]


def generate_request_id(run_id: str, case_id: str, operation: str, attempt: int = 0) -> str:
    return f"{run_id}-{case_id}-{operation}-{attempt}-{secrets.token_hex(4)}"


def build_plan(
    run_id: str,
    profile_name: str,
    scenarios: dict[str, Any],
    merchant_filter: str | None = None,
    buyer_filter: str | None = None,
    amount_override: str | None = None,
    currency_override: str | None = None,
    max_cases: int | None = None,
    confirm_full_matrix: bool = False,
) -> list[Case]:
    profile = scenarios.get("profiles", {}).get(profile_name)
    if not profile:
        raise ValueError(f"Unknown profile: {profile_name}")

    if profile_name == "full" and not confirm_full_matrix:
        raise ValueError("--confirm-full-matrix is required to run the full matrix.")

    merchants = profile.get("merchants", [])
    if merchant_filter:
        merchants = [m for m in merchants if m == merchant_filter.upper()]
    buyers = profile.get("buyers_per_merchant", [])

    base_amounts = resolve_amounts(profile_name, scenarios)
    cases: list[Case] = []
    index = 0
    for merchant in merchants:
        currency = currency_override or currency_for_country(merchant)
        amounts = resolve_amount_for_country(base_amounts, merchant, currency)
        if amount_override:
            amounts = [amount_override]

        for buyer_template in buyers:
            buyer = merchant if buyer_template == "same" else buyer_template
            if buyer_filter and buyer != buyer_filter.upper():
                continue
            # De-duplicate same-country combos at the plan level.
            if (merchant, buyer) in [
                (c.merchant_country, c.buyer_country) for c in cases if c.merchant_country == merchant
            ]:
                continue
            for amount in amounts:
                index += 1
                case_id = f"{profile_name}-{merchant}-{buyer}-{index}"
                cases.append(
                    Case(
                        case_id=case_id,
                        run_id=run_id,
                        merchant_country=merchant,
                        buyer_country=buyer,
                        amount=amount,
                        currency=currency,
                        product_id="",
                        variant_id="",
                    )
                )
                if max_cases and len(cases) >= max_cases:
                    return cases
    return cases


def enrich_plan_with_products(plan: list[Case], adapter: QuoteAdapter) -> list[Case]:
    """Resolve the product/variant for each planned case using current capabilities."""
    for case in plan:
        scenario = adapter.resolve_scenario(case.merchant_country)
        if scenario:
            case.product_id = scenario["product_id"]
            case.variant_id = scenario["variant_id"]
    return plan


def plan_summary(plan: list[Case]) -> dict[str, Any]:
    merchants = sorted({c.merchant_country for c in plan})
    buyers = sorted({c.buyer_country for c in plan})
    return {
        "run_id": plan[0].run_id if plan else None,
        "case_count": len(plan),
        "merchants": merchants,
        "buyers": buyers,
        "currencies": sorted({c.currency for c in plan}),
    }
