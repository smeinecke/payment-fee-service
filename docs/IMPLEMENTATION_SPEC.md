# Implementation Specification

## Goal

Provide a stateless HTTP service that estimates public standard transaction fees for multiple payment providers using externally maintained, schema-versioned fee repositories.

## Non-goals

- crawling pricing websites
- authoritative settlement reproduction
- negotiated account pricing
- tax calculation
- live foreign-exchange rates
- chargeback or dispute probability modelling

## Data repositories

### PayPal

Runtime calculation input is `json/core-fees.json`. `json/index.json` supplies integrity and provenance. Per-country documents remain diagnostic artifacts and are not loaded on the quote hot path.

### Stripe

Runtime calculation input is `json/core-fees.json`. `json/payment-methods.json` supports discovery. `json/index.json` supplies integrity and provenance. Per-market documents remain diagnostic artifacts.

## Public contract

`POST /v1/quotes` accepts a discriminated union keyed by `provider`. Shared fields are amount, account country, customer country, and settlement currency. Provider-specific context is nested under `transaction` using the same typed models as the `payment-fee` library. The service calls `PaymentFeeEngine.quote()` directly, so HTTP and library results are identical.

Responses always contain:

- gross amount
- processing fee
- net amount
- independently visible fee components
- matched rule identifiers and quality metadata
- assumptions
- exact data snapshot provenance

## Money

All source values and API amounts are parsed as `Decimal`. Binary floating-point is forbidden in fee mathematics. Each fee component is rounded using the currency exponent and `ROUND_HALF_UP`; total and net values are then checked for consistency.

## Availability

Market-level `partial` status is informational. Quote availability is decided at requested-category or matched-rule level. Unclassified required rules are not used.

## PayPal compiler

1. Resolve account country to the consolidated country entry.
2. Resolve transaction category to one of the derived commercial-fee objects.
3. Require a percentage.
4. Follow `fixed_fee_reference` and require a fixed fee for the transaction currency.
5. Treat same-country payer transactions as domestic.
6. For international payers, match a published region label directly or require `payment.surcharge_region`.
7. Compile base and optional surcharge components.

Currency conversion spread is intentionally not included in the processing fee because a spread alone is insufficient without an exchange-rate source, conversion direction, and confirmation that conversion occurs.

## Stripe compiler

1. Resolve the account country to a consolidated market.
2. Keep transaction rules only; exclude payout and dispute dimensions.
3. Reject unclassified rules and rules with no numerical fee.
4. Scope explicit payment-method rules to the requested method.
5. Scope generic card-dimension rules to card payments.
6. Apply populated dimensions, amount thresholds, and conditions.
7. Surface missing constrained dimensions as `INSUFFICIENT_TRANSACTION_CONTEXT`.
8. Resolve a most-specific base rule.
9. Reject conflicting equal-specificity base rules.
10. Add matching contextual generic rules.
11. Reject unknown behaviors and condition operators.

## Startup and readiness

Each provider loads independently. With `fail_startup_on_data_error=false`, the process remains live and exposes provider-specific data errors. `/health/ready` is successful only when all configured providers loaded successfully. With strict startup enabled, any load failure terminates startup.

## Extensibility

A new provider may use a completely different source schema. It must compile selected source rules into the shared mathematical rule model and return a common quote response.

## Security and operations

- Run as a non-root container user.
- Pin production data URLs to immutable commit SHAs.
- Keep administrative data refresh outside the public API; restart or redeploy with a new pinned snapshot.
- Set HTTP timeouts for remote snapshot loading.
- Do not log full payment metadata unless an operator explicitly adds redacted structured logging.
