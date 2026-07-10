# Payment Fee Service

A provider-neutral FastAPI service that estimates transaction fees from versioned public fee datasets.

Initial providers:

- PayPal, using `smeinecke/paypal-fee-data`
- Stripe, using `smeinecke/stripe-fee-data`

The service never crawls provider websites during a quote request. It loads validated JSON snapshots, selects provider-specific rules, calculates with `Decimal`, and returns source provenance with every result.

> This is an estimation service based on unofficial public datasets. It is not an authoritative billing, settlement, tax, or accounting system. Negotiated pricing and provider-side rounding may differ.

## Architecture

```text
paypal-fee-data ──► PayPal adapter ──┐
                                    ├──► shared Decimal calculator ──► REST API
stripe-fee-data ──► Stripe adapter ──┘
```

The public request and response contracts are stable across providers. Storage schemas and rule matching remain provider-specific, so future adapters do not require either source repository to adopt a common schema.

## Features

- `POST /v1/quotes` with discriminated PayPal and Stripe requests
- exact decimal calculations and ISO 4217-aware rounding
- fixed, percentage, basis-point, minimum, maximum, and additive fee components
- PayPal transaction categories and international surcharge-region handling
- Stripe multidimensional matching for payment method, card origin/region/tier, channel, recurrence, billing type, currencies, thresholds, and published conditions
- fail-closed behavior for ambiguous rules, missing context, unsupported conditions, unknown behaviors, and currency mismatches
- rule-level availability rather than rejecting every market marked `partial`
- market, capability, data-status, liveness, and readiness endpoints
- local or HTTPS-based snapshot loading
- JSON Schema validation at startup
- Docker image, CLI, tests, and GitHub Actions

## Requirements

- Python 3.12+

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

## Data configuration

Providers are configured as a dynamic `providers` dictionary. Each key is a provider ID that maps to a provider module (`payment_fee_service.providers.<id>`), and each value contains `data_url`, `data_path`, `data_ref`, and `enabled`.

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

## Validate configured data

```bash
payment-fee-service validate-data
```

The command exits unsuccessfully if either configured provider snapshot cannot be loaded or validated.

## API

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
    "payment": {"transaction_type": "standard_commercial"}
  }'
```

Supported PayPal transaction types are exposed per market and can include:

- `standard_commercial`
- `goods_and_services`
- `micropayments`
- `donations`
- `nonprofit`

For an international transaction, pass the exact PayPal surcharge-region label published for the merchant market when the customer country cannot be matched directly:

```json
{
  "provider": "paypal",
  "amount": {"value": "100.00", "currency": "EUR"},
  "account_country": "DE",
  "customer_country": "US",
  "payment": {
    "transaction_type": "standard_commercial",
    "surcharge_region": "OTHER"
  }
}
```

The service deliberately does not guess provider-specific regions.

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
    "payment": {
      "method": "card",
      "channel": "online",
      "recurring": false,
      "card": {
        "origin": "domestic",
        "region": "eea",
        "tier": "standard"
      }
    }
  }'
```

Additional published Stripe condition dimensions can be supplied under `payment.context`:

```json
{
  "payment": {
    "method": "example_method",
    "context": {
      "custom_dimension_from_dataset": "value"
    }
  }
}
```

Unknown condition operators are rejected rather than ignored.

### Example response

```json
{
  "provider": "stripe",
  "status": "estimated",
  "amount": {"value": "100.00", "currency": "EUR"},
  "processing_fee": {"value": "1.75", "currency": "EUR"},
  "net_amount": {"value": "98.25", "currency": "EUR"},
  "components": [
    {
      "type": "processing",
      "label": "Standard EEA cards",
      "amount": "1.75",
      "currency": "EUR",
      "rate_percentage": "1.5",
      "fixed_amount": "0.25",
      "source_rule_id": "..."
    }
  ],
  "matched_rules": [
    {
      "rule_id": "...",
      "classification_status": "classified",
      "confidence": 1.0,
      "exactness": "exact",
      "source_url": "https://stripe.com/..."
    }
  ],
  "assumptions": [
    "Public standard pricing was used; negotiated or IC++ pricing is not represented."
  ],
  "data": {
    "provider": "stripe",
    "schema_version": 1,
    "market": "DE",
    "content_sha256": "...",
    "source_urls": ["https://stripe.com/..."],
    "source_updated_at": null,
    "data_ref": "<pinned commit>"
  }
}
```

### Discovery and health

```text
GET /v1/providers
GET /v1/providers/{provider}/markets
GET /v1/providers/{provider}/markets/{country}/capabilities
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
      "missing_fields": ["payment.card.region"],
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

## Rule selection safeguards

### PayPal

The adapter:

1. Selects the merchant market by ISO country code.
2. Selects the requested derived transaction category.
3. Resolves the category's `fixed_fee_reference` instead of assuming a field.
4. Requires an exact fixed fee for the transaction currency.
5. Adds an international surcharge only when the payer is international and its region can be resolved explicitly.
6. Returns the index hash, source URL, source timestamp, schema version, and configured data ref.

Missing fixed fees never become zero.

### Stripe

The adapter:

1. Limits candidates to the merchant account country.
2. Excludes payout and dispute rules from transaction quotes.
3. Excludes unclassified rules and rules without fee values.
4. Applies every populated rule dimension and threshold.
5. Evaluates published conditions using a small fail-closed operator set.
6. Requires missing context before selecting a constrained rule.
7. Chooses the most specific payment-method base rule.
8. Rejects equally specific base rules with different financial values.
9. Adds only contextual generic surcharges and prevents card surcharges from leaking into non-card quotes.
10. Rejects unsupported `behavior` values.

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
