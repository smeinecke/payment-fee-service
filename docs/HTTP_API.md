# HTTP API

The Payment Fee Service exposes one public API version: `/v1`.

## Endpoints

```text
POST /v1/quotes
GET  /v1/providers
GET  /v1/providers/{provider}/markets
GET  /v1/providers/{provider}/markets/{account_country}/capabilities
GET  /v1/providers/{provider}/markets/{account_country}/quote-schema
GET  /v1/data/status
POST /v1/data/refresh
GET  /health/live
GET  /health/ready
GET  /docs
GET  /redoc
GET  /docs/openapi.json
```

There are no `/v2` endpoints and no legacy `payment` request object.

## Request contract

`POST /v1/quotes` accepts a discriminated union keyed by `provider`. Shared fields are `amount`, `account_country`, `customer_country`, and `settlement_currency`. Provider-specific context is nested under `transaction`.

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

### PayPal international quote

For an international payer, pass the exact PayPal surcharge-region label published for the merchant market when the customer country cannot be matched directly:

```bash
curl -sS http://localhost:8000/v1/quotes \
  -H 'content-type: application/json' \
  -d '{
    "provider": "paypal",
    "amount": {"value": "100.00", "currency": "EUR"},
    "account_country": "AD",
    "customer_country": "US",
    "settlement_currency": "EUR",
    "transaction": {
      "product_id": "other_commercial",
      "variant_id": "standard",
      "transaction_region": "international",
      "payer_region": "OTHER"
    }
  }'
```

### Additional Stripe context

Published Stripe condition dimensions that are not captured by typed transaction fields can be supplied under `transaction.context`:

```json
{
  "transaction": {
    "context": {
      "custom_dimension_from_dataset": "value"
    }
  }
}
```

Unknown condition operators are rejected rather than ignored.

## Response contract

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
      "label": "Domestic card payments",
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
      "classification_status": "calculable",
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

## Discovery

```bash
curl http://localhost:8000/v1/providers
curl http://localhost:8000/v1/providers/stripe/markets
curl http://localhost:8000/v1/providers/stripe/markets/DE/capabilities
curl http://localhost:8000/v1/providers/stripe/markets/DE/quote-schema
curl http://localhost:8000/v1/data/status
```

## Refresh

```bash
curl -X POST http://localhost:8000/v1/data/refresh \
  -H "Authorization: Bearer <admin_token>"
```

The endpoint is disabled when `admin_token` is not configured.

## Errors

Failures use a stable envelope:

```json
{
  "error": {
    "code": "QUOTE_NOT_AVAILABLE",
    "message": "...",
    "details": {}
  }
}
```

Common status codes:

- `200` — success
- `400` — malformed request
- `404` — unknown provider/market or disabled refresh endpoint
- `409` — ambiguous fee rules
- `422` — quote cannot be computed with supplied context
- `503` — provider data unavailable or failed validation
