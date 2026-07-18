from decimal import Decimal

import pytest
from payment_fee import PaymentFeeEngine, PayPalQuoteRequest
from payment_fee.errors import InsufficientTransactionContext
from payment_fee.models import Money, PayPalTransaction


def test_paypal_domestic_quote(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount=Money(value="100.00", currency="EUR"),
        account_country="DE",
        customer_country="DE",
        settlement_currency="EUR",
        transaction=PayPalTransaction(
            product_id="other_commercial",
            variant_id="standard",
            transaction_region="domestic",
        ),
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("3.38")


def test_paypal_us_checkout_quote(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount=Money(value="100.00", currency="USD"),
        account_country="US",
        customer_country="US",
        settlement_currency="USD",
        transaction=PayPalTransaction(
            product_id="paypal_checkout",
            variant_id="standard",
            payment_method="paypal",
            transaction_region="domestic",
        ),
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("3.98")


def test_paypal_international_quote(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount=Money(value="100.00", currency="EUR"),
        account_country="AD",
        customer_country="US",
        settlement_currency="EUR",
        transaction=PayPalTransaction(
            product_id="other_commercial",
            variant_id="standard",
            payment_method="card",
            transaction_region="international",
            payer_region="OTHER",
        ),
    )
    quote = engine.quote(request)
    assert quote.processing_fee.value == Decimal("4.75")


def test_paypal_missing_surcharge_region(engine: PaymentFeeEngine) -> None:
    request = PayPalQuoteRequest(
        provider="paypal",
        amount=Money(value="100.00", currency="EUR"),
        account_country="BE",
        customer_country="US",
        settlement_currency="EUR",
        transaction=PayPalTransaction(
            product_id="other_commercial",
            variant_id="standard",
            transaction_region="international",
        ),
    )
    with pytest.raises(InsufficientTransactionContext) as exc_info:
        engine.quote(request)
    assert "transaction.payer_region" in exc_info.value.details["missing_fields"]
