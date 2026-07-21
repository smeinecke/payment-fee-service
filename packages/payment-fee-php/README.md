# smeinecke/payment-fee (PHP)

Native PHP implementation of the `payment-fee` calculation library.

## Requirements

- PHP >= 8.2
- Composer
- `ext-json`

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

## Development

```bash
composer install
composer test
composer analyse
composer format-check
composer audit
```

## Status

This package is a native PHP port of the Python `payment-fee` library. It consumes the same provider datasets and aims for identical normalized quote results. The implementation is a work in progress.

## Structural notes

- `QuoteRequestFactory` is a PHP-specific helper for building request objects from
  untyped arrays. Python and TypeScript keep the equivalent request-building logic
  inline in their engine/model layers, so this factory is an intentional PHP-only
  structural convenience, not drift from the other ports.
