from decimal import Decimal

import pytest
from payment_fee import PaymentFeeEngine, StripeQuoteRequest
from payment_fee.errors import InsufficientTransactionContext
from payment_fee.models import Money, StripeTransaction


def test_stripe_card_quote(engine: PaymentFeeEngine) -> None:
    request = StripeQuoteRequest(
        provider="stripe",
        amount=Money(value="100.00", currency="EUR"),
        account_country="DE",
        customer_country="DE",
        settlement_currency="EUR",
        transaction=StripeTransaction(
            product_id="payments",
            variant_id="online_domestic_cards",
            payment_method="card",
            channel="online",
            pricing_tier="standard",
            card={"origin": "domestic", "region": "domestic", "tier": "standard"},
            context={"transaction_type": "charge"},
        ),
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("1.75")


def test_stripe_sepa_bank_transfer_capped(engine: PaymentFeeEngine) -> None:
    request = StripeQuoteRequest(
        provider="stripe",
        amount=Money(value="1000.00", currency="EUR"),
        account_country="DE",
        customer_country="DE",
        settlement_currency="EUR",
        transaction=StripeTransaction(
            product_id="sepa_bank_transfer",
            variant_id="standard",
            payment_method="sepa_bank_transfer",
            channel="online",
            context={"transaction_type": "charge"},
        ),
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("5.00")


def test_stripe_missing_card_region(engine: PaymentFeeEngine) -> None:
    request = StripeQuoteRequest(
        provider="stripe",
        amount=Money(value="100.00", currency="EUR"),
        account_country="DE",
        customer_country="US",
        settlement_currency="EUR",
        transaction=StripeTransaction(
            product_id="payments",
            variant_id="online_domestic_cards",
            payment_method="card",
            channel="online",
            card={"origin": "domestic"},
        ),
    )
    with pytest.raises(InsufficientTransactionContext) as exc_info:
        engine.quote(request)
    assert "transaction.card.region" in exc_info.value.details["missing_fields"]
