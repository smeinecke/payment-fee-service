# Library API

The `payment-fee` package is a reusable Python library for estimating public standard payment-processing fees. It can be used directly without the HTTP service.

## Loading data

```python
from payment_fee import PaymentFeeEngine

engine = PaymentFeeEngine.from_paths(
    paypal="/path/to/paypal-fee-data",
    stripe="/path/to/stripe-fee-data",
)
```

## Quotes

```python
from payment_fee import StripeQuoteRequest
from payment_fee.models import Money, StripeTransaction

quote = engine.quote(
    StripeQuoteRequest(
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
        ),
    )
)
print(quote.processing_fee.value)
```

A request can also be passed as a plain dictionary:

```python
quote = engine.quote({
    "provider": "paypal",
    "amount": {"value": "100.00", "currency": "EUR"},
    "account_country": "DE",
    "customer_country": "DE",
    "settlement_currency": "EUR",
    "transaction": {
        "product_id": "other_commercial",
        "variant_id": "standard",
        "transaction_region": "domestic",
    },
})
```

## Discovery

```python
engine.providers()
engine.markets("stripe")
engine.capabilities("stripe", "DE")
engine.quote_schema("stripe", "DE")
engine.data_status()
```

## Validation

Both `from_paths` and `from_documents` accept a `validate` flag. For `from_documents`, pass the matching schemas under a `schemas` key:

```python
engine = PaymentFeeEngine.from_documents(
    paypal={
        "core": core_doc,
        "index": index_doc,
        "schemas": {
            "core": core_schema,
            "index": index_schema,
        },
    },
    stripe={
        "core": core_doc,
        "index": index_doc,
        "payment_methods": pm_doc,
        "schemas": {
            "core": core_schema,
            "index": index_schema,
            "payment_methods": pm_schema,
        },
    },
    validate=True,
)
```

If `validate=True` and the schemas are missing, `DatasetValidationError` is raised.

## Contract audit

The library exposes an `audit_contract` helper that walks every calculable rule and produces counters:

```python
from payment_fee.audit import audit_contract

result = audit_contract(engine)
assert result.paypal_calculable_rules_skipped == 0
assert result.stripe_calculable_rules_skipped == 0
```

Counters include `*_parsed`, `*_skipped`, `*_context_required`, `unsupported_fee_components`, `unknown_condition_dimensions`, `unknown_condition_operators`, and `unresolved_schedule_references`.

## Exceptions

All library errors subclass `PaymentFeeError` and carry a `code`, `message`, and `details` dictionary:

- `UnknownProvider`
- `UnknownMarket`
- `ProviderDataUnavailable`
- `QuoteNotAvailable`
- `InsufficientTransactionContext`
- `AmbiguousFeeRules`
- `CurrencyMismatch`
- `UnsupportedFeeShape`
- `DatasetValidationError`
