from decimal import Decimal

import pytest

from payment_fee_service.domain.errors import InsufficientContextError
from payment_fee_service.domain.models import PayPalQuoteRequest
from payment_fee_service.service import QuoteService


def test_paypal_domestic_quote(registry) -> None:
    request = PayPalQuoteRequest.model_validate(
        {
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "payment": {"transaction_type": "standard_commercial"},
        }
    )
    quote = QuoteService(registry).calculate(request)
    assert quote.processing_fee.value == Decimal("3.38")
    assert quote.data.content_sha256 == "paypal-de-fixture"


def test_paypal_international_requires_region(registry) -> None:
    request = PayPalQuoteRequest.model_validate(
        {
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "US",
            "payment": {"transaction_type": "standard_commercial"},
        }
    )
    with pytest.raises(InsufficientContextError):
        QuoteService(registry).calculate(request)


def test_paypal_international_quote(registry) -> None:
    request = PayPalQuoteRequest.model_validate(
        {
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "US",
            "payment": {"transaction_type": "standard_commercial", "surcharge_region": "OTHER"},
        }
    )
    quote = QuoteService(registry).calculate(request)
    assert quote.processing_fee.value == Decimal("5.37")
    assert len(quote.components) == 2
