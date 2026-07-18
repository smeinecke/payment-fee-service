from decimal import Decimal

import pytest
from payment_fee import PaymentFeeEngine, StripeQuoteRequest
from payment_fee_service.domain.errors import InsufficientContextError
from payment_fee_service.domain.models import StripeQuoteRequest as ServiceStripeQuoteRequest
from payment_fee_service.service import QuoteService


def test_stripe_card_quote_v2(engine: PaymentFeeEngine) -> None:
    request = StripeQuoteRequest(
        provider="stripe",
        amount={"value": "100.00", "currency": "EUR"},
        account_country="DE",
        customer_country="DE",
        settlement_currency="EUR",
        transaction={
            "product_id": "payments",
            "variant_id": "online_domestic_cards",
            "payment_method": "card",
            "channel": "online",
            "pricing_tier": "standard",
            "card": {"origin": "domestic", "region": "domestic", "tier": "standard"},
            "context": {"transaction_type": "charge"},
        },
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("1.75")


def test_stripe_card_quote_v1(engine: PaymentFeeEngine) -> None:
    request = ServiceStripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "payment": {
                "method": "card",
                "channel": "online",
                "card": {"origin": "domestic", "region": "eea", "tier": "standard"},
            },
        }
    )
    quote = QuoteService(engine).calculate(request)
    assert quote.processing_fee.value == Decimal("1.75")


def test_stripe_sepa_bank_transfer_capped(engine: PaymentFeeEngine) -> None:
    request = ServiceStripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "1000.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "payment": {"method": "sepa_bank_transfer", "channel": "online"},
        }
    )
    quote = QuoteService(engine).calculate(request)
    assert quote.processing_fee.value == Decimal("5.00")


def test_stripe_missing_card_region(engine: PaymentFeeEngine) -> None:
    request = ServiceStripeQuoteRequest.model_validate(
        {
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "US",
            "payment": {"method": "card", "channel": "online", "card": {"origin": "domestic"}},
        }
    )
    with pytest.raises(InsufficientContextError) as exc_info:
        QuoteService(engine).calculate(request)
    assert "transaction.card.region" in exc_info.value.details["missing_fields"]
