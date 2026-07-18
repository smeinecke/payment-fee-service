# Payment Fee Service

A provider-neutral FastAPI service that estimates transaction fees from versioned public fee datasets.

Initial providers:

- PayPal, using `smeinecke/paypal-fee-data`
- Stripe, using `smeinecke/stripe-fee-data`

The service never crawls provider websites during a quote request. It loads validated JSON snapshots, selects provider-specific rules, calculates with `Decimal`, and returns source provenance with every result.

> This is an estimation service based on unofficial public datasets. It is not an authoritative billing, settlement, tax, or accounting system. Negotiated pricing and provider-side rounding may differ.

## Architecture

```text
                    packages/payment-fee
paypal-fee-data ──►  ┌──────────────┐  ──┐
                     │ PayPal rules │    │
stripe-fee-data ──►  │ Stripe rules │    ▼  Decimal calculator, schemas, provenance
                     └──────────────┘  ───►  services/payment-fee-service  ──►  FastAPI /v1
```

`payment-fee` is a reusable Python library with minimal dependencies (Pydantic, jsonschema, ISO currency metadata). It owns all provider parsing, rule compilation, and fee calculation. `payment-fee-service` is a thin FastAPI wrapper that handles HTTP concerns, snapshot loading, and refresh. The public request and response contracts are stable across providers. Storage schemas and rule matching remain provider-specific, so future adapters do not require either source repository to adopt a common schema.

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

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
uv sync --all-packages --extra dev
cp .env.example .env
```

The repository is a `uv` workspace with two packages:

- `packages/payment-fee` — reusable calculation library
- `services/payment-fee-service` — FastAPI service

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
  json/payment-methods.json
  schemas/core-fees-v1.schema.json
  schemas/index-v1.schema.json
  schemas/payment-methods-v1.schema.json
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

```bash
pytest
```

The suite covers:

- percentage and fixed fees
- zero-decimal currencies
- PayPal domestic and international quotes
- required PayPal surcharge regions
- Stripe base and additive rules
- missing Stripe dimensions
- capped percentage fees
- API discovery, health, quote, and structured-error behavior
- library/HTTP parity

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
