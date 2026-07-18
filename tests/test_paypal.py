from decimal import Decimal

import pytest
from payment_fee import PaymentFeeEngine, PayPalQuoteRequest
from payment_fee_service.domain.errors import InsufficientContextError
from payment_fee_service.domain.models import PayPalQuoteRequest as ServicePayPalQuoteRequest
from payment_fee_service.service import QuoteService


def test_paypal_domestic_quote_v1(engine: PaymentFeeEngine) -> None:
    request = ServicePayPalQuoteRequest.model_validate(
        {
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "payment": {"transaction_type": "standard_commercial"},
        }
    )
    quote = QuoteService(engine).calculate(request)
    assert quote.processing_fee.value == Decimal("3.38")


def test_paypal_us_checkout_quote_v2(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount={"value": "100.00", "currency": "USD"},
        account_country="US",
        customer_country="US",
        settlement_currency="USD",
        transaction={
            "product_id": "paypal_checkout",
            "variant_id": "standard",
            "payment_method": "paypal",
            "transaction_region": "domestic",
        },
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("3.98")


def test_paypal_international_quote_v2(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount={"value": "100.00", "currency": "EUR"},
        account_country="AD",
        customer_country="US",
        settlement_currency="EUR",
        transaction={
            "product_id": "other_commercial",
            "variant_id": "standard",
            "payment_method": "card",
            "transaction_region": "international",
            "payer_region": "OTHER",
        },
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("4.75")


def test_paypal_missing_surcharge_region(engine: PaymentFeeEngine) -> None:
    request = ServicePayPalQuoteRequest.model_validate(
        {
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "BE",
            "customer_country": "US",
            "settlement_currency": "EUR",
            "payment": {"transaction_type": "standard_commercial"},
        }
    )
    with pytest.raises(InsufficientContextError) as exc_info:
        QuoteService(engine).calculate(request)
    assert "transaction.payer_region" in exc_info.value.details["missing_fields"]
