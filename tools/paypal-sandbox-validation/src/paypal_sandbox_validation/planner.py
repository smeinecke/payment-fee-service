from __future__ import annotations

import secrets
import uuid
from typing import Any

from paypal_sandbox_validation.configuration import (
    currency_for_country,
    resolve_amount_for_country,
    resolve_amounts,
)
from paypal_sandbox_validation.models import Case, CaseStatus
from paypal_sandbox_validation.quote_adapter import QuoteAdapter

_SURCHARGE_BUYER_CANDIDATES = ["GB", "DE", "JP", "AU", "CH", "FR", "IT"]


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
    """Resolve product/variant, expected payer region and surcharge for each case."""
    for case in plan:
        scenario = adapter.resolve_scenario(case.merchant_country)
        if scenario:
            case.product_id = scenario["product_id"]
            case.variant_id = scenario["variant_id"]
        try:
            quote = adapter.build_quote(
                case.merchant_country,
                case.buyer_country,
                case.amount,
                case.currency,
            )
            case.quote = quote
            request = quote.get("_request", {})
            transaction = request.get("transaction", {})
            case.expected_payer_region = transaction.get("payer_region")
            components = quote.get("components", [])
            surcharge_components = [c for c in components if c.get("type") == "surcharge"]
            case.expected_surcharge_components = len(surcharge_components)
            if surcharge_components:
                case.expected_surcharge_amount = surcharge_components[0].get("amount")
        except Exception:
            # Leave quote empty; run-time will attempt again and record the failure.
            pass
    return plan


def ensure_surcharge_case(plan: list[Case], adapter: QuoteAdapter) -> list[Case]:
    """If no cross-border case in a smoke-style plan carries a surcharge, add one."""
    if not plan:
        return plan

    domestic = [c for c in plan if c.merchant_country == c.buyer_country]
    cross_border = [c for c in plan if c.merchant_country != c.buyer_country]

    # Domestic case must not have a surcharge.
    for case in domestic:
        if case.expected_surcharge_components:
            case.quote = None
            case.expected_surcharge_components = 0
            case.expected_surcharge_amount = None

    if any(c.expected_surcharge_components > 0 for c in cross_border):
        return plan

    merchant = plan[0].merchant_country
    amount = plan[0].amount
    currency = plan[0].currency
    existing_buyers = {c.buyer_country for c in plan}
    index = max(int(c.case_id.split("-")[-1]) for c in plan)
    prefix = "-".join(plan[0].case_id.split("-")[:2])

    for buyer in _SURCHARGE_BUYER_CANDIDATES:
        if buyer in existing_buyers:
            continue
        if buyer == merchant:
            continue
        try:
            quote = adapter.build_quote(merchant, buyer, amount, currency)
            components = quote.get("components", [])
            surcharge_components = [c for c in components if c.get("type") == "surcharge"]
            if surcharge_components:
                index += 1
                request = quote.get("_request", {})
                transaction = request.get("transaction", {})
                new_case = Case(
                    case_id=f"{prefix}-{buyer}-{index}",
                    run_id=plan[0].run_id,
                    merchant_country=merchant,
                    buyer_country=buyer,
                    amount=amount,
                    currency=currency,
                    product_id=quote.get("_scenario", {}).get("product_id", ""),
                    variant_id=quote.get("_scenario", {}).get("variant_id", ""),
                    status=CaseStatus.PLANNED,
                    quote=quote,
                    expected_payer_region=transaction.get("payer_region"),
                    expected_surcharge_components=len(surcharge_components),
                    expected_surcharge_amount=surcharge_components[0].get("amount"),
                )
                plan.append(new_case)
                return plan
        except Exception:
            continue

    # If we cannot find a surcharge-bearing case, document the fact but keep the plan.
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
