# Dataset Adapter Specification

All implementations consume the same provider datasets. The adapter layer translates provider-native JSON documents into an internal rule representation and validates schema versions and unknown fields.

## Supported schema versions

`contracts/dataset-support.json` is the source of truth for supported dataset schema versions. Every implementation generates native constants from this manifest and rejects unsupported future schema versions explicitly.

```json
{
  "paypal": {
    "core-fees": [1],
    "index": [1]
  },
  "stripe": {
    "core-fees": [1],
    "index": [1]
  }
}
```

## Loading modes

All libraries support in-memory documents and filesystem paths.

| Language   | Filesystem      | In-memory        |
|------------|-----------------|------------------|
| Python     | `from_paths`    | `from_documents` |
| PHP        | `fromPaths`     | `fromDocuments`  |
| TypeScript | `fromPaths`     | `fromDocuments`  |

Applications and the HTTP service own remote downloads, pinning, retries, and refresh. Core packages do not perform HTTP downloads.

## Fail-closed validation

Adapters must reject:

* Unknown top-level or nested fields in dataset documents (where `additionalProperties` is forbidden in the provider models).
* Unknown condition dimensions.
* Unknown condition operators.
* Unsupported fee component types.
* Unresolved schedule references.
* Unsupported schema versions.

Adapters must not silently skip calculable rules.

## PayPal document layout

```text
paypal-fee-data/
  json/core-fees.json
  json/index.json
  schemas/core-fees-v1.schema.json
  schemas/index-v1.schema.json
```

`core-fees.json` contains an array of country entries. Each entry has `country_code`, `derived_status`, and `derived` data including `transaction_fee_rules`, `fixed_fee_schedules`, `international_surcharge_schedules`, and `maximum_fee_schedules`.

`index.json` contains metadata for each country including `content_sha256`, `source_url`, `source_updated_at`, and `locale`.

## Stripe document layout

```text
stripe-fee-data/
  json/core-fees.json
  json/index.json
  schemas/core-fees-v1.schema.json
  schemas/index-v1.schema.json
```

`core-fees.json` contains an array of market entries. Each entry has `account_country`, `rules`, and coverage metadata.

`index.json` contains metadata for each market.

## Schedule registry (PayPal)

PayPal schedules are referenced by ID strings. The schedule ID may contain selectors such as `applies_to_markets=DE` or `pricing_plan=foo`. Implementations must parse these selectors explicitly and match them against the request context. Schedule-prefix heuristics are not allowed.

## Audit counters

Every adapter must produce the following counters for a contract audit:

```text
paypal_calculable_rules_total
paypal_calculable_rules_parsed
paypal_calculable_rules_skipped
paypal_context_required
stripe_calculable_rules_total
stripe_calculable_rules_parsed
stripe_calculable_rules_skipped
stripe_context_required
unknown_fields
unknown_condition_dimensions
unknown_condition_operators
unsupported_fee_components
unresolved_schedule_references
```

Acceptance for committed data revisions:

```text
paypal_calculable_rules_skipped = 0
stripe_calculable_rules_skipped = 0
unknown_fields = 0
unknown_condition_operators = 0
unresolved_schedule_references = 0
```
