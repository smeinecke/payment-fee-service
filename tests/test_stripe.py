from decimal import Decimal

import pytest

from payment_fee_service.domain.errors import InsufficientContextError
from payment_fee_service.domain.models import StripeQuoteRequest
from payment_fee_service.service import QuoteService


def test_stripe_card_quote(registry) -> None:
    request = StripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "payment": {
                "method": "card",
                "channel": "online",
                "card": {"origin": "domestic", "region": "eea"},
            },
        }
    )
    quote = QuoteService(registry).calculate(request)
    assert quote.processing_fee.value == Decimal("1.75")
    assert [rule.rule_id for rule in quote.matched_rules] == ["stripe-de-card-eea"]


def test_stripe_international_additive_quote(registry) -> None:
    request = StripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "US",
            "payment": {
                "method": "card",
                "channel": "online",
                "card": {"origin": "international", "region": "eea"},
            },
        }
    )
    quote = QuoteService(registry).calculate(request)
    assert quote.processing_fee.value == Decimal("3.25")
    assert len(quote.components) == 2


def test_stripe_requires_card_region(registry) -> None:
    request = StripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "payment": {"method": "card", "channel": "online", "card": {"origin": "domestic"}},
        }
    )
    with pytest.raises(InsufficientContextError) as error:
        QuoteService(registry).calculate(request)
    assert "payment.card.region" in error.value.details["missing_fields"]


def test_stripe_capped_percentage(registry) -> None:
    request = StripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "1000.00", "currency": "EUR"},
            "account_country": "DE",
            "payment": {"method": "sepa_debit"},
        }
    )
    quote = QuoteService(registry).calculate(request)
    assert quote.processing_fee.value == Decimal("5.00")
