# Python Library

`payment-fee` is the original Python implementation of the calculation library.

## Installation

```bash
pip install payment-fee
```

## Usage

```python
from payment_fee import PaymentFeeEngine

engine = PaymentFeeEngine.from_paths(
    paypal="/data/paypal-fee-data",
    stripe="/data/stripe-fee-data",
)

result = engine.quote({
    "provider": "stripe",
    "amount": {"value": "100.00", "currency": "EUR"},
    "account_country": "DE",
    "settlement_currency": "EUR",
    "transaction": {
        "product_id": "payments",
        "variant_id": "online_domestic_cards",
        "payment_method": "card",
        "channel": "online",
    },
})
```

## Implementation notes

* Uses `Decimal` for all financial arithmetic.
* Loads canonical currency metadata from `contracts/currencies.json`.
* Validates unknown fields, operators, and fee components fail-closed.
* Produces normalized JSON matching `contracts/api/quote-response-v1.schema.json`.
