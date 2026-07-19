from __future__ import annotations

import json
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


def _pilot_amount_for_country(country: str) -> str:
    """Return the standard pilot amount in the merchant's principal currency."""
    if currency_for_country(country) == "JPY":
        return "1000"
    return "10.00"


def _quote_signature(quote: dict[str, Any] | None) -> str:
    """Stable signature for grouping quotes by schedule and component shape."""
    if not quote:
        return "unavailable"
    meta = quote.get("_schedule_metadata") or {}
    return json.dumps(
        {
            "base_rule_id": meta.get("base_rule_id"),
            "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
            "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
            "component_signature": meta.get("component_signature"),
        },
        sort_keys=True,
        default=str,
    )


def _case_from_quote(
    run_id: str,
    case_id: str,
    merchant_country: str,
    buyer_country: str,
    quote: dict[str, Any],
    rationale: str,
    distinct: bool = False,
) -> Case:
    meta = quote.get("_schedule_metadata") or {}
    request = quote.get("_request", {})
    transaction = request.get("transaction", {})
    components = quote.get("components", []) or []
    surcharge_components = [c for c in components if c.get("type") == "surcharge"]
    return Case(
        case_id=case_id,
        run_id=run_id,
        merchant_country=merchant_country,
        buyer_country=buyer_country,
        amount=quote.get("amount", {}).get("value", ""),
        currency=quote.get("amount", {}).get("currency", ""),
        product_id=quote.get("_scenario", {}).get("product_id", ""),
        variant_id=quote.get("_scenario", {}).get("variant_id", ""),
        status=CaseStatus.PLANNED,
        quote=quote,
        expected_payer_region=transaction.get("payer_region"),
        expected_surcharge_components=len(surcharge_components),
        expected_surcharge_amount=surcharge_components[0].get("amount") if surcharge_components else None,
        pilot_metadata={
            "selection_rationale": rationale,
            "is_distinct_schedule": distinct,
            "has_surcharge": meta.get("surcharge_percentage") is not None,
            **_pilot_quote_metadata(quote),
        },
    )


def _pilot_quote_metadata(quote: dict[str, Any] | None) -> dict[str, Any]:
    if not quote:
        return {}
    meta = quote.get("_schedule_metadata") or {}
    return {
        "base_rule_id": meta.get("base_rule_id"),
        "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
        "base_percentage": meta.get("base_percentage"),
        "fixed_amount": meta.get("fixed_amount"),
        "surcharge_percentage": meta.get("surcharge_percentage"),
        "predicted_total_fee": meta.get("predicted_total_fee"),
        "payer_region": meta.get("payer_region"),
        "component_signature": meta.get("component_signature"),
    }


def build_surcharge_pilot_plan(
    run_id: str,
    merchant_country: str,
    buyer_countries: set[str],
    adapter: QuoteAdapter,
    amount: str = "10.00",
    currency: str = "USD",
    candidate_buyers: list[str] | None = None,
) -> tuple[list[Case], bool]:
    """Build a two-case pilot: domestic control + first nonzero-surcharge international candidate."""
    candidate_buyers = candidate_buyers or ["DE", "GB", "JP", "AU", "BR", "HK", "IL", "ZA"]

    # Domestic control
    domestic_quote = adapter.build_quote(merchant_country, merchant_country, amount, currency)
    domestic_case = _case_from_quote(
        run_id=run_id,
        case_id=f"surcharge-{merchant_country}-{merchant_country}-1",
        merchant_country=merchant_country,
        buyer_country=merchant_country,
        quote=domestic_quote,
        rationale="domestic_control",
    )
    domestic_case.pilot_metadata["domestic_predicted_surcharge"] = "0"
    plan: list[Case] = [domestic_case]

    domestic_signature = _quote_signature(domestic_quote)

    selected: tuple[str, dict[str, Any]] | None = None
    for buyer in candidate_buyers:
        if buyer == merchant_country or buyer not in buyer_countries:
            continue
        try:
            quote = adapter.build_quote(merchant_country, buyer, amount, currency)
        except Exception:
            continue
        meta = quote.get("_schedule_metadata") or {}
        if (
            quote.get("status") == "exact_for_public_rate"
            and meta.get("surcharge_percentage") is not None
            and _quote_signature(quote) != domestic_signature
        ):
            selected = (buyer, quote)
            break

    if selected:
        buyer, quote = selected
        international_case = _case_from_quote(
            run_id=run_id,
            case_id=f"surcharge-{merchant_country}-{buyer}-2",
            merchant_country=merchant_country,
            buyer_country=buyer,
            quote=quote,
            rationale="nonzero_surcharge_candidate",
            distinct=True,
        )
        plan.append(international_case)
        return plan, True

    return plan, False


def build_regional_pilot_plan(
    run_id: str,
    merchant_countries: list[str],
    buyer_countries: set[str],
    adapter: QuoteAdapter,
    max_cases: int = 24,
) -> tuple[list[Case], dict[str, Any]]:
    """Build a regional pilot with at most two cases per merchant (domestic + distinct schedule)."""
    plan: list[Case] = []
    summary: dict[str, Any] = {
        "merchants_evaluated": [],
        "no_distinct_schedule_candidates": [],
        "capability_unavailable": [],
        "library_not_calculable": [],
    }

    for merchant in merchant_countries:
        if len(plan) >= max_cases:
            break

        amount = _pilot_amount_for_country(merchant)
        currency = currency_for_country(merchant)

        summary["merchants_evaluated"].append(merchant)

        quotes_by_buyer: dict[str, dict[str, Any] | None] = {}
        signatures: dict[str, list[str]] = {}
        for buyer in sorted(buyer_countries):
            try:
                quote = adapter.build_quote(merchant, buyer, amount, currency)
            except Exception:
                continue
            if quote.get("status") != "exact_for_public_rate":
                continue
            quotes_by_buyer[buyer] = quote
            signature = _quote_signature(quote)
            signatures.setdefault(signature, []).append(buyer)

        if merchant not in quotes_by_buyer:
            # Merchant cannot be calculated in its own currency: create a placeholder that
            # will be skipped at run-time with the appropriate reconciliation status.
            plan.append(
                Case(
                    case_id=f"regional-{merchant}-{merchant}-1",
                    run_id=run_id,
                    merchant_country=merchant,
                    buyer_country=merchant,
                    amount=amount,
                    currency=currency,
                    product_id="",
                    variant_id="",
                    status=CaseStatus.PLANNED,
                    pilot_metadata={"selection_rationale": "merchant_not_calculable"},
                )
            )
            summary["capability_unavailable"].append(merchant)
            continue

        domestic_quote = quotes_by_buyer[merchant]
        assert domestic_quote is not None
        domestic_signature = _quote_signature(domestic_quote)
        plan.append(
            _case_from_quote(
                run_id=run_id,
                case_id=f"regional-{merchant}-{merchant}-1",
                merchant_country=merchant,
                buyer_country=merchant,
                quote=domestic_quote,
                rationale="domestic_same_country",
            )
        )

        if len(plan) >= max_cases:
            break

        # Prefer a buyer whose signature differs and carries a surcharge.
        distinct_buyers: list[str] = []
        for signature, buyers in signatures.items():
            if signature != domestic_signature:
                distinct_buyers.extend(buyers)

        selected_buyer: str | None = None
        selected_quote: dict[str, Any] | None = None
        for buyer in distinct_buyers:
            quote = quotes_by_buyer.get(buyer)
            if not quote:
                continue
            meta = quote.get("_schedule_metadata") or {}
            if meta.get("surcharge_percentage") is not None:
                selected_buyer = buyer
                selected_quote = quote
                break
        if not selected_buyer:
            # Any differing signature is acceptable.
            for buyer in distinct_buyers:
                quote = quotes_by_buyer.get(buyer)
                if quote:
                    selected_buyer = buyer
                    selected_quote = quote
                    break

        if selected_buyer and selected_quote:
            plan.append(
                _case_from_quote(
                    run_id=run_id,
                    case_id=f"regional-{merchant}-{selected_buyer}-2",
                    merchant_country=merchant,
                    buyer_country=selected_buyer,
                    quote=selected_quote,
                    rationale="distinct_schedule_prefer_surcharge",
                    distinct=True,
                )
            )
        else:
            summary["no_distinct_schedule_candidates"].append(merchant)

    return plan, summary


def build_diagnostic_plan(
    run_id: str,
    merchant_country: str,
    diagnostic_amounts: list[str],
    control_buyers: list[str],
    currency: str,
    adapter: QuoteAdapter,
    max_new_captures: int = 5,
) -> list[Case]:
    """Build a bounded diagnostic plan for a specific merchant.

    Generates cases for the requested amounts against the primary buyer, plus
    a single-amount control case for each listed buyer country.  The total
    number of captures is capped at ``max_new_captures``.
    """
    plan: list[Case] = []
    index = 0

    # Try to infer the primary diagnostic buyer from the adapter; default to AU.
    primary_buyer = control_buyers[0] if control_buyers else "AU"

    for amount in diagnostic_amounts:
        if len(plan) >= max_new_captures:
            break
        index += 1
        case_id = f"diag-{merchant_country}-{primary_buyer}-{index}"
        try:
            quote = adapter.build_quote(merchant_country, primary_buyer, amount, currency)
        except Exception:
            quote = None
        case = _case_from_quote(
            run_id=run_id,
            case_id=case_id,
            merchant_country=merchant_country,
            buyer_country=primary_buyer,
            quote=quote or {},
            rationale="diagnostic_amount_series",
        )
        plan.append(case)

    # Use the first amount for control buyers; skip the primary buyer to avoid duplicates.
    control_amount = diagnostic_amounts[0] if diagnostic_amounts else "10.00"
    for buyer in control_buyers:
        if len(plan) >= max_new_captures:
            break
        if buyer == primary_buyer:
            continue
        index += 1
        case_id = f"diag-{merchant_country}-{buyer}-{index}"
        try:
            quote = adapter.build_quote(merchant_country, buyer, control_amount, currency)
        except Exception:
            quote = None
        case = _case_from_quote(
            run_id=run_id,
            case_id=case_id,
            merchant_country=merchant_country,
            buyer_country=buyer,
            quote=quote or {},
            rationale="diagnostic_control",
        )
        plan.append(case)

    return plan
