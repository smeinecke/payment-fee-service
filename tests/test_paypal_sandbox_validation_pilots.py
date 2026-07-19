from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from paypal_sandbox_validation.configuration import currency_for_country
from paypal_sandbox_validation.models import Case
from paypal_sandbox_validation.planner import (
    build_regional_pilot_plan,
    build_surcharge_pilot_plan,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter, validate_amount_for_currency


def _fee_data_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "paypal-fee-data")


def test_surcharge_pilot_plan_selects_nonzero_surcharge() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", _fee_data_path())
    adapter = QuoteAdapter()
    run_id = "pilot-run-1"
    buyer_countries = {"US", "DE", "GB", "JP", "AU", "BR", "HK", "IL", "ZA"}

    plan, found = build_surcharge_pilot_plan(
        run_id=run_id,
        merchant_country="US",
        buyer_countries=buyer_countries,
        adapter=adapter,
    )

    assert found is True
    assert len(plan) == 2
    domestic = next(c for c in plan if c.buyer_country == "US")
    international = next(c for c in plan if c.buyer_country != "US")

    assert domestic.pilot_metadata["has_surcharge"] is False
    assert international.pilot_metadata["has_surcharge"] is True
    assert international.pilot_metadata["surcharge_percentage"] is not None
    assert international.pilot_metadata["is_distinct_schedule"] is True


def test_surcharge_pilot_plan_reports_no_candidate() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", _fee_data_path())
    adapter = QuoteAdapter()
    run_id = "pilot-run-2"

    plan, found = build_surcharge_pilot_plan(
        run_id=run_id,
        merchant_country="US",
        buyer_countries={"US"},
        adapter=adapter,
    )

    assert found is False
    assert len(plan) == 1
    assert plan[0].buyer_country == "US"


def test_quote_adapter_rejects_fractional_jpy() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", _fee_data_path())
    adapter = QuoteAdapter()

    # Use a calculable merchant (US) with a zero-decimal JPY amount.  The
    # fractional guard must reject before the engine is asked to calculate.
    with pytest.raises(ValueError):
        adapter.build_quote("US", "US", "1000.01", "JPY")

    with pytest.raises(ValueError):
        validate_amount_for_currency("123.45", "JPY")


def _mock_quote(merchant: str, buyer: str, amount: str, currency: str) -> dict[str, Any]:
    """Return a deterministic quote for regional pilot grouping tests."""
    base_meta = {
        "base_rule_id": f"paypal:{merchant}:other_commercial:standard:base",
        "fixed_fee_schedule_id": "other_commercial",
        "international_surcharge_schedule_id": None,
        "base_percentage": "2.9",
        "fixed_amount": "0.30",
        "surcharge_percentage": None,
        "predicted_total_fee": "0.59",
        "payer_region": merchant,
        "component_signature": [("processing", "0.59")],
    }

    if buyer == merchant:
        # Domestic case: no surcharge.
        return {
            "status": "exact_for_public_rate",
            "amount": {"value": amount, "currency": currency},
            "processing_fee": {"value": "0.59", "currency": currency},
            "components": [{"type": "processing", "amount": "0.59", "rate_percentage": "2.9", "fixed_amount": "0.30"}],
            "_schedule_metadata": base_meta,
            "_request": {"transaction": {"payer_region": merchant}},
            "_scenario": {"product_id": "other_commercial", "variant_id": "standard"},
        }

    # International case with surcharge for some buyers.
    surcharge = buyer in {"DE", "FR"}
    component_signature = [("processing", "0.59"), ("surcharge", "0.15")] if surcharge else [("processing", "0.59")]
    components = [
        {"type": t, "amount": a, "rate_percentage": "1.5" if t == "surcharge" else "2.9", "fixed_amount": None}
        for t, a in component_signature
    ]
    meta = {
        **base_meta,
        "surcharge_percentage": "1.5" if surcharge else None,
        "international_surcharge_schedule_id": "other_commercial" if surcharge else None,
        "predicted_total_fee": "0.74" if surcharge else "0.59",
        "payer_region": "EEA" if surcharge else "OTHER",
        "component_signature": component_signature,
    }
    return {
        "status": "exact_for_public_rate",
        "amount": {"value": amount, "currency": currency},
        "processing_fee": {"value": meta["predicted_total_fee"], "currency": currency},
        "components": components,
        "_schedule_metadata": meta,
        "_request": {"transaction": {"payer_region": meta["payer_region"]}},
        "_scenario": {"product_id": "other_commercial", "variant_id": "standard"},
    }


def test_regional_pilot_plan_groups_and_prefers_surcharge() -> None:
    adapter = SimpleNamespace(build_quote=_mock_quote)
    run_id = "regional-run-1"
    merchant_countries = ["US", "GB"]
    buyer_countries = {"US", "GB", "DE", "FR", "AU"}

    plan, summary = build_regional_pilot_plan(
        run_id=run_id,
        merchant_countries=merchant_countries,
        buyer_countries=buyer_countries,
        adapter=adapter,  # type: ignore[arg-type]
        max_cases=24,
    )

    # Two cases per merchant.
    assert len(plan) == 4
    by_merchant: dict[str, list[Case]] = {}
    for case in plan:
        by_merchant.setdefault(case.merchant_country, []).append(case)

    for merchant in merchant_countries:
        cases = by_merchant[merchant]
        assert len(cases) == 2
        domestic = next(c for c in cases if c.buyer_country == merchant)
        international = next(c for c in cases if c.buyer_country != merchant)
        assert domestic.pilot_metadata["selection_rationale"] == "domestic_same_country"
        assert international.pilot_metadata["is_distinct_schedule"] is True
        assert international.pilot_metadata["has_surcharge"] is True
        # DE and FR are the only surcharging buyers in the mock.
        assert international.buyer_country in {"DE", "FR"}


def test_regional_pilot_plan_reports_no_distinct_schedule_candidate() -> None:
    """When all buyers share the same signature, only a domestic case is emitted."""

    def uniform_quote(merchant: str, buyer: str, amount: str, currency: str) -> dict[str, Any]:
        return {
            "status": "exact_for_public_rate",
            "amount": {"value": amount, "currency": currency},
            "processing_fee": {"value": "0.59", "currency": currency},
            "components": [{"type": "processing", "amount": "0.59", "rate_percentage": "2.9", "fixed_amount": "0.30"}],
            "_schedule_metadata": {
                "base_rule_id": f"paypal:{merchant}:other_commercial:standard:base",
                "fixed_fee_schedule_id": "other_commercial",
                "international_surcharge_schedule_id": None,
                "base_percentage": "2.9",
                "fixed_amount": "0.30",
                "surcharge_percentage": None,
                "predicted_total_fee": "0.59",
                "payer_region": merchant,
                "component_signature": [("processing", "0.59")],
            },
            "_request": {"transaction": {"payer_region": merchant}},
            "_scenario": {"product_id": "other_commercial", "variant_id": "standard"},
        }

    adapter = SimpleNamespace(build_quote=uniform_quote)
    run_id = "regional-run-2"

    plan, summary = build_regional_pilot_plan(
        run_id=run_id,
        merchant_countries=["US"],
        buyer_countries={"US", "GB"},
        adapter=adapter,  # type: ignore[arg-type]
    )

    assert len(plan) == 1
    assert plan[0].buyer_country == "US"
    assert summary["no_distinct_schedule_candidates"] == ["US"]


def test_pilot_amount_for_country_jpy_is_integer() -> None:
    from paypal_sandbox_validation.planner import _pilot_amount_for_country

    assert _pilot_amount_for_country("JP") == "1000"
    assert _pilot_amount_for_country("US") == "10.00"
    assert currency_for_country("JP") == "JPY"
