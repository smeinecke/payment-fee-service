# Payment Fee Service

A polyglot provider-neutral fee estimation project. It provides reusable native libraries in Python, PHP, and TypeScript, plus an optional FastAPI service.

Initial providers:

- PayPal, using `smeinecke/paypal-fee-data`
- Stripe, using `smeinecke/stripe-fee-data`

The libraries load validated JSON snapshots, select provider-specific rules, calculate with arbitrary-precision decimal arithmetic, and return source provenance with every result.

> This is an estimation service based on unofficial public datasets. It is not an authoritative billing, settlement, tax, or accounting system. Negotiated pricing and provider-side rounding may differ.

## Repository layout

```text
payment-fee-service/
├── contracts/           canonical JSON schemas, specs, currency metadata, conformance cases
├── packages/
│   ├── payment-fee/     Python library
│   ├── payment-fee-php/ PHP library (smeinecke/payment-fee)
│   └── payment-fee-typescript/ TypeScript library (@smeinecke/payment-fee)
├── services/
│   └── payment-fee-service/ FastAPI /v1 service
└── tools/
    ├── conformance/              cross-language differential runner
    └── paypal-sandbox-validation/  PayPal Sandbox harness and qualification
```

The canonical public contract lives in `contracts/api`. All three libraries consume the same provider datasets and produce the same normalized JSON results. The HTTP service is an optional wrapper around the Python library.

## Features

- `POST /v1/quotes` with discriminated PayPal and Stripe requests using provider-native `transaction` objects
- exact decimal calculations and ISO 4217-aware rounding
- fixed, percentage, basis-point, minimum, maximum, and additive fee components
- explicit PayPal schedule registry (schedule IDs, market selectors, and pricing-plan selectors)
- Stripe multidimensional matching for payment method, card origin/region/tier, channel, recurrence, billing type, currencies, thresholds, and published conditions
- fail-closed dataset adapters that reject unknown upstream fields before Pydantic validation
- fail-closed behavior for ambiguous rules, missing context, unsupported conditions, unknown behaviors, and currency mismatches
- rule-level availability rather than rejecting every market marked `partial`
- market, capability, quote-schema, data-status, liveness, and readiness endpoints
- local or HTTPS-based snapshot loading with optional JSON Schema validation
- contract audit helpers with counters for skipped rules, unknown dimensions, and unresolved schedule references
- Docker image, CLI, tests, and GitHub Actions

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) for Python development
- PHP 8.2+ and Composer for PHP development
- Node.js 20+ and npm for TypeScript development

## Installation

### Python

```bash
uv sync --all-packages --extra dev
cp .env.example .env
```

The `uv` workspace includes:

- `packages/payment-fee` — reusable Python calculation library
- `services/payment-fee-service` — FastAPI service

### PHP

```bash
cd packages/payment-fee-php
composer install
```

### TypeScript

```bash
cd packages/payment-fee-typescript
npm ci
```

## Data configuration

Providers are configured as a dynamic `providers` dictionary. Each value contains `data_url`, `data_path`, `data_ref`, and `enabled`.

### Configuration file

The recommended way is `PAYMENT_FEE_CONFIG_FILE` pointing to a JSON file:

```json
{
  "refresh_interval_seconds": 86400,
  "admin_token": "change-me-in-production",
  "providers": {
    "paypal": {
      "data_url": "https://raw.githubusercontent.com/smeinecke/paypal-fee-data/<commit-sha>",
      "data_ref": "<commit-sha>",
      "enabled": true
    },
    "stripe": {
      "data_url": "https://raw.githubusercontent.com/smeinecke/stripe-fee-data/<commit-sha>",
      "data_ref": "<commit-sha>",
      "enabled": true
    }
  }
}
```

For production, pin each URL to a commit SHA.

### Environment overrides

Top-level settings and provider values can be overridden via `PAYMENT_FEE_*` variables. For example:

```env
PAYMENT_FEE_PROVIDERS={"paypal": {"data_url": "...", "data_ref": "main"}, "stripe": {"data_url": "...", "data_ref": "main"}}
PAYMENT_FEE_REFRESH_INTERVAL_SECONDS=86400
PAYMENT_FEE_ADMIN_TOKEN=change-me-in-production
```

### Local snapshots

Clone or mount the two data repositories and point the service at their repository roots:

```json
{
  "providers": {
    "paypal": {"data_path": "/data/paypal-fee-data", "data_ref": "local"},
    "stripe": {"data_path": "/data/stripe-fee-data", "data_ref": "local"}
  }
}
```

Local paths take precedence over URLs.

The loaders consume:

```text
PayPal
  json/core-fees.json
  json/index.json
  schemas/core-fees-v1.schema.json
  schemas/index-v1.schema.json

Stripe
  json/core-fees.json
  json/index.json
  schemas/core-fees-v1.schema.json
  schemas/index-v1.schema.json
```

## Run

```bash
payment-fee-service serve --host 0.0.0.0 --port 8000
```

or with a config file:

```bash
payment-fee-service --config-file /etc/payment-fee-service/config.json serve --host 0.0.0.0 --port 8000
```

or:

```bash
uvicorn payment_fee_service.app:app --reload
```

### Periodic refresh

When `refresh_interval_seconds` is greater than `0`, the service reloads provider data in the background. The default is `86400` (one day).

### Manual refresh endpoint

If `admin_token` is configured, `POST /v1/data/refresh` reloads all providers immediately:

```bash
curl -X POST http://localhost:8000/v1/data/refresh \
  -H "Authorization: Bearer <admin_token>"
```

If `admin_token` is not configured, the endpoint is disabled.

### systemd

Install the unit and config file:

```bash
sudo cp systemd/payment-fee-service.service /etc/systemd/system/
sudo cp systemd/config.json /etc/payment-fee-service/config.json
sudo systemctl daemon-reload
sudo systemctl enable --now payment-fee-service
```

OpenAPI documentation is available at `/docs` and `/redoc`.

## HTTP API

The canonical public API is `/v1`. See `docs/HTTP_API.md` for full endpoint examples and `docs/LIBRARY_API.md` for direct library usage.

### Stripe card quote

```bash
curl -sS http://localhost:8000/v1/quotes \
  -H 'content-type: application/json' \
  -d '{
    "provider": "stripe",
    "amount": {"value": "100.00", "currency": "EUR"},
    "account_country": "DE",
    "customer_country": "DE",
    "settlement_currency": "EUR",
    "transaction": {
      "product_id": "payments",
      "variant_id": "online_domestic_cards",
      "payment_method": "card",
      "channel": "online",
      "pricing_tier": "standard",
      "card": {"origin": "domestic", "region": "domestic", "tier": "standard"}
    }
  }'
```

### PayPal domestic quote

```bash
curl -sS http://localhost:8000/v1/quotes \
  -H 'content-type: application/json' \
  -d '{
    "provider": "paypal",
    "amount": {"value": "100.00", "currency": "EUR"},
    "account_country": "DE",
    "customer_country": "DE",
    "settlement_currency": "EUR",
    "transaction": {
      "product_id": "other_commercial",
      "variant_id": "standard",
      "transaction_region": "domestic"
    }
  }'
```

### Discovery and health

```text
GET /v1/providers
GET /v1/providers/{provider}/markets
GET /v1/providers/{provider}/markets/{account_country}/capabilities
GET /v1/providers/{provider}/markets/{account_country}/quote-schema
GET /v1/data/status
GET /health/live
GET /health/ready
```

### Errors

Provider and calculation failures use a stable envelope:

```json
{
  "error": {
    "code": "INSUFFICIENT_TRANSACTION_CONTEXT",
    "message": "Additional transaction context is required to select an applicable fee rule.",
    "details": {
      "missing_fields": ["transaction.card.region"],
      "candidate_rule_ids": ["..."]
    }
  }
}
```

Important codes:

- `UNKNOWN_PROVIDER`
- `UNKNOWN_MARKET`
- `PROVIDER_DATA_UNAVAILABLE`
- `QUOTE_NOT_AVAILABLE`
- `INSUFFICIENT_TRANSACTION_CONTEXT`
- `AMBIGUOUS_FEE_RULES`

## Validate configured data

```bash
payment-fee-service validate-data
```

The command exits unsuccessfully if either configured provider snapshot cannot be loaded or validated.

## Libraries

### Python

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

See `docs/PYTHON_LIBRARY.md`.

### PHP

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

See `docs/PHP_LIBRARY.md`.

### TypeScript

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

See `docs/TYPESCRIPT_LIBRARY.md`.

## Docker image

A pre-built image is published to GitHub Container Registry on every push:

```bash
docker run -p 8000:8000 \
  -e PAYMENT_FEE_CONFIG_FILE=/etc/payment-fee-service/config.json \
  -v $(pwd)/config.json:/etc/payment-fee-service/config.json:ro \
  ghcr.io/smeinecke/payment-fee-service:latest
```

Build locally:

```bash
make docker-build
docker run -p 8000:8000 ghcr.io/smeinecke/payment-fee-service:local
```

## Tests

### Python

```bash
make test-python
```

### PHP

```bash
cd packages/payment-fee-php
composer install
composer test
```

### TypeScript

```bash
cd packages/payment-fee-typescript
npm ci
npm test
```

### Cross-language conformance

```bash
make test-conformance
```

The full suite covers:

- percentage and fixed fees
- zero-decimal and three-decimal currencies
- midpoint rounding, caps, and minimums
- PayPal domestic and international quotes
- required PayPal surcharge regions
- Stripe base and additive rules
- missing Stripe dimensions
- capped percentage fees
- API discovery, health, quote, and structured-error behavior
- library/HTTP parity
- cross-language normalized result parity

## Add another provider

Implement the `FeeProvider` protocol:

```python
class FeeProvider(Protocol):
    provider_id: str

    def compile_rules(self, request: QuoteRequest) -> CompiledFeePlan: ...
    def markets(self) -> list[MarketInfo]: ...
    def capabilities(self, account_country: str) -> CapabilityInfo: ...
```

Add a discriminated provider request model, a repository loader, a provider-specific matcher/compiler, and contract tests. Keep provider dimensions out of the shared calculator.

See `docs/IMPLEMENTATION_SPEC.md` and `docs/ADDING_A_PROVIDER.md`.

## Supported dataset schema versions

| Provider | Document        | Supported versions |
|----------|-----------------|--------------------|
| PayPal   | core-fees       | 1                  |
| PayPal   | index           | 1                  |
| Stripe   | core-fees       | 1                  |
| Stripe   | index           | 1                  |

See `contracts/dataset-support.json`.

## Conformance guarantees

All three native libraries consume the same provider datasets and canonical JSON contracts. The cross-language conformance suite in `contracts/conformance` defines expected behavior. Implementations must produce identical normalized quote results and contract-audit counters for committed data revisions.

## Versioning

All libraries share major and minor versions for coordinated contract releases. Patch releases may differ per language for implementation fixes that do not change observable results.

Current versions:

- `payment-fee` Python — 0.4.0
- `smeinecke/payment-fee` PHP — 0.4.0
- `@smeinecke/payment-fee` TypeScript — 0.4.0
- `payment-fee-service` — 0.4.0

## Runtime requirements

- Python 3.12+
- PHP 8.2+
- Node.js 20+

## Limitations

* Public pricing data is unofficial and may not match negotiated rates.
* Provider-side rounding, tax, and settlement timing may differ.
* PHP and TypeScript provider adapters are under construction; not all cases are passing yet.
* The HTTP service is Python-based and optional.

### PayPal Sandbox validation

PayPal Sandbox Business profiles can display environment/account-specific rates
that differ from public production pricing. Sandbox observations are kept
separate from production public-rate conformance. The fee service continues to
use public production-oriented datasets by default.

The validation harness records three distinct evidence scopes:

* `production_public_pricing` — public production-facing PayPal fee pages.
* `sandbox_profile_pricing` — authenticated Sandbox Business account pricing.
* `observed_transaction_pricing` — fees captured from Sandbox transactions.

See [tools/paypal-sandbox-validation/README.md](tools/paypal-sandbox-validation/README.md)
and [docs/PAYPAL_SANDBOX_VALIDATION.md](docs/PAYPAL_SANDBOX_VALIDATION.md).
