# @smeinecke/payment-fee (TypeScript)

Native TypeScript implementation of the `payment-fee` calculation library.

## Requirements

- Node.js >= 20
- npm

## Installation

```bash
npm install @smeinecke/payment-fee
```

## Usage

```ts
import { PaymentFeeEngine } from "@smeinecke/payment-fee";

const engine = await PaymentFeeEngine.fromPaths({
  paypal: "/data/paypal-fee-data",
  stripe: "/data/stripe-fee-data",
});

const result = engine.quote({
  provider: "stripe",
  amount: { value: "100.00", currency: "EUR" },
  account_country: "DE",
  settlement_currency: "EUR",
  transaction: {
    product_id: "payments",
    variant_id: "online_domestic_cards",
    payment_method: "card",
    channel: "online",
  },
});
```

For Node.js-specific utilities (filesystem loading, streams):

```ts
import { PaymentFeeEngine } from "@smeinecke/payment-fee/node";
```

## Development

```bash
npm ci
npm run lint
npm run format:check
npm run typecheck
npm test
npm run build
npm pack --dry-run
npm audit
```

## Status

This package is a native TypeScript port of the Python `payment-fee` library. It consumes the same provider datasets and aims for identical normalized quote results. The implementation is a work in progress.
