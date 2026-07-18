# TypeScript Library

`@smeinecke/payment-fee` is the native TypeScript implementation of the calculation library.

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

You can also load documents directly from in-memory objects:

```ts
const engine = await PaymentFeeEngine.fromDocuments({
  paypal: { core: paypalCore },
  stripe: { core: stripeCore },
});
```

For Node.js-specific utilities:

```ts
import { PaymentFeeEngine } from "@smeinecke/payment-fee/node";
```

## Implementation notes

* Uses `decimal.js` for arbitrary-precision decimal arithmetic.
* The main entry point is runtime-neutral and supports `fromDocuments`.
* The `node` entry point may add filesystem loading, path handling, and Node streams.
* Produces normalized JSON matching `contracts/api/quote-response-v1.schema.json`.
* Stripe condition matching follows the Python reference implementation, including `eq` operators that accept a list of candidate values and additive rules that apply when their conditions are fully satisfied.

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

Unit tests live in `packages/payment-fee-typescript/tests` and cover the engine, calculator, and money model.
