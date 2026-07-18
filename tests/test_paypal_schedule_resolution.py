from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from payment_fee import PaymentFeeEngine
from payment_fee.errors import QuoteNotAvailable
from payment_fee.models import Money, PayPalQuoteRequest, PayPalTransaction


def _money(amount: str, currency: str = "USD") -> Money:
    return Money(value=Decimal(amount), currency=currency)


def _request(account_country: str, product_id: str, **kwargs: Any) -> PayPalQuoteRequest:
    return PayPalQuoteRequest(
        provider="paypal",
        amount=_money("100"),
        account_country=account_country,
        customer_country=account_country,
        transaction=PayPalTransaction(product_id=product_id, **kwargs),
    )


def _core_with_country(
    country_code: str,
    rules: list[dict[str, Any]],
    fixed_schedules: dict[str, dict[str, str]],
    surcharge_schedules: dict[str, Any] | None = None,
    max_schedules: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    surcharge_schedules = surcharge_schedules or {}
    max_schedules = max_schedules or {}
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "countries": [
            {
                "country_code": country_code,
                "iso_country_code": country_code,
                "paypal_market_code": country_code,
                "derived_status": "complete",
                "derived": {
                    "status": "complete",
                    "transaction_fee_rules": rules,
                    "fixed_fee_schedules": {sid: {"entries": entries} for sid, entries in fixed_schedules.items()},
                    "international_surcharge_schedules": {
                        sid: {"entries": entries} for sid, entries in surcharge_schedules.items()
                    },
                    "maximum_fee_schedules": {sid: {"entries": entries} for sid, entries in max_schedules.items()},
                    "currency_conversion": None,
                },
            }
        ],
    }


class TestExactScheduleReference:
    def test_exact_schedule_id_used(self) -> None:
        core = _core_with_country(
            "US",
            rules=[
                {
                    "id": "goods_and_services",
                    "calculation_status": "calculable",
                    "percentage": "2.9",
                    "fixed_fee_schedule": "goods_and_services",
                }
            ],
            fixed_schedules={"goods_and_services": {"USD": "0.30"}},
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        response = engine.quote(_request("US", "goods_and_services", transaction_region="domestic"))
        assert response.processing_fee.value == Decimal("3.20")

    def test_similar_prefix_is_not_selected(self) -> None:
        core = _core_with_country(
            "US",
            rules=[
                {
                    "id": "advanced_card_payments",
                    "calculation_status": "calculable",
                    "percentage": "2.9",
                    "fixed_fee_schedule": "advanced_card_payments",
                }
            ],
            fixed_schedules={
                "advanced_card_payments": {"USD": "0.30"},
                "advanced_card_payments_eterminal": {"USD": "5.00"},
            },
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        response = engine.quote(_request("US", "advanced_card_payments", transaction_region="domestic"))
        assert response.processing_fee.value == Decimal("3.20")


class TestMarketSpecificSchedule:
    def test_market_selector_schedule(self) -> None:
        core = _core_with_country(
            "AD",
            rules=[
                {
                    "id": "advanced_card_payments",
                    "variant_id": "standard",
                    "calculation_status": "calculable",
                    "percentage": "2.9",
                    "fixed_fee_schedule": "advanced_card_payments__applies_to_markets=sg",
                    "conditions": {"applies_to_markets": ["SG"], "transaction_region": "international"},
                }
            ],
            fixed_schedules={
                "advanced_card_payments__applies_to_markets=sg": {"SGD": "0.50"},
            },
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        response = engine.quote(
            PayPalQuoteRequest(
                provider="paypal",
                amount=_money("100", "SGD"),
                account_country="AD",
                customer_country="SG",
                transaction=PayPalTransaction(
                    product_id="advanced_card_payments",
                    variant_id="standard",
                    transaction_region="international",
                ),
            )
        )
        assert response.amount.currency == "SGD"


class TestPricingPlanSchedule:
    def test_pricing_plan_selector_schedule(self) -> None:
        core = _core_with_country(
            "US",
            rules=[
                {
                    "id": "advanced_card_payments",
                    "variant_id": "standard_card",
                    "calculation_status": "calculable",
                    "percentage": "2.4",
                    "fixed_fee_schedule": "advanced_card_payments__pricing_plan=blended",
                    "conditions": {"pricing_plan": "blended", "transaction_region": "domestic"},
                }
            ],
            fixed_schedules={
                "advanced_card_payments__pricing_plan=blended": {"USD": "0.10"},
            },
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        response = engine.quote(
            _request(
                "US",
                "advanced_card_payments",
                variant_id="standard_card",
                pricing_plan="blended",
                transaction_region="domestic",
            )
        )
        assert response.processing_fee.value == Decimal("2.50")


class TestNoMatchingSchedule:
    def test_missing_schedule_raises_quote_not_available(self) -> None:
        core = _core_with_country(
            "US",
            rules=[
                {
                    "id": "goods_and_services",
                    "calculation_status": "calculable",
                    "percentage": "2.9",
                    "fixed_fee_schedule": "missing_schedule",
                }
            ],
            fixed_schedules={},
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        with pytest.raises(QuoteNotAvailable):
            engine.quote(_request("US", "goods_and_services", transaction_region="domestic"))


class TestDirectFixedPlusSchedule:
    def test_direct_fixed_and_schedule_amount_additive(self) -> None:
        core = _core_with_country(
            "US",
            rules=[
                {
                    "id": "card_verification",
                    "calculation_status": "calculable",
                    "fee_components": [
                        {"type": "fixed_amount", "amount": "0.15", "currency": "USD"},
                    ],
                    "fixed_fee_schedule": "card_verification",
                }
            ],
            fixed_schedules={"card_verification": {"USD": "0.25"}},
        )
        engine = PaymentFeeEngine.from_documents(paypal=core)
        response = engine.quote(_request("US", "card_verification", transaction_region="domestic"))
        assert response.processing_fee.value == Decimal("0.40")
