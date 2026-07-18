# PHP Library

`smeinecke/payment-fee` is the native PHP implementation of the calculation library.

## Installation

```bash
composer require smeinecke/payment-fee
```

## Usage

```php
use Smeinecke\PaymentFee\PaymentFeeEngine;
use Smeinecke\PaymentFee\Model\StripeQuoteRequest;
use Smeinecke\PaymentFee\Model\Money;
use Smeinecke\PaymentFee\Model\StripeTransaction;

$engine = PaymentFeeEngine::fromPaths(
    paypal: '/data/paypal-fee-data',
    stripe: '/data/stripe-fee-data',
);

$result = $engine->quote(
    new StripeQuoteRequest(
        amount: new Money('100.00', 'EUR'),
        accountCountry: 'DE',
        settlementCurrency: 'EUR',
        transaction: new StripeTransaction(
            productId: 'payments',
            variantId: 'online_domestic_cards',
            paymentMethod: 'card',
            channel: 'online',
        ),
    ),
);
```

## Implementation notes

* Uses `brick/math` for arbitrary-precision decimal arithmetic.
* Loads canonical currency metadata from `contracts/currencies.json`.
* Exposes typed exceptions matching the Python hierarchy.
* Produces normalized JSON matching `contracts/api/quote-response-v1.schema.json`.

## Development

```bash
composer install
composer test
composer analyse
composer format-check
composer audit
```
