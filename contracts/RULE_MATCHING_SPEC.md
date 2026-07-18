# Rule Matching Specification

## Overview

Rule matching is provider-specific but follows a shared selection algorithm. The output of rule matching is a `CompiledFeePlan` containing an ordered list of `ExecutableFeeRule` objects and provenance metadata.

## Selection algorithm

1. **Select provider** by the `provider` field of the request.
2. **Select account market** by `account_country`.
3. **Exclude non-calculable, informational, and conflicting rules.**
   * Skip rules whose `calculation_status` is not calculable.
   * Skip rules that are purely informational or marked as unsupported/non-calculable in the provider data.
4. **Match product and variant.**
   * For PayPal, match `transaction.product_id` and `transaction.variant_id` against the rule `id` and `variant_id`.
   * For Stripe, match `transaction.product_id` and `transaction.variant_id` against the rule `product_id` and `variant_id`.
5. **Evaluate every structured condition.**
   * Conditions are dimension/operator/value tuples.
   * Supported operators: `eq`, `==`, `equals`, `ne`, `!=`, `not_equals`, `in`, `not_in`, `nin`, `gt`, `gte`, `lt`, `lte`.
   * Numeric comparisons use arbitrary-precision decimal arithmetic.
   * String comparisons are case-sensitive unless the dimension is explicitly case-normalized (currencies uppercase, countries uppercase).
6. **Detect missing required context.**
   * If a condition references a dimension that is `None`/unset in the request context and the condition is not satisfied, the rule is not applicable.
   * If multiple otherwise-identical rules differ only by a missing dimension, raise `InsufficientTransactionContext` with the missing field path and candidate rule IDs.
7. **Select the most specific applicable base rule.**
   * Specificity is measured by the number of matched conditions and the depth of the matched context.
   * The base rule (behavior `base`) with the highest specificity wins.
8. **Reject equally specific rules with different financial definitions.**
   * If two or more base rules match with the same specificity but different percentage, fixed amount, or component type, raise `AmbiguousFeeRules`.
9. **Select applicable additive components.**
   * Additive rules (behavior `additive`, `surcharge`, `additional_fee`) that match the request are appended after the base rule in deterministic order.
10. **Reject unsupported applicable rules instead of skipping them.**
    * If a matched rule has an unsupported fee shape or component type, raise `UnsupportedFeeShape` rather than silently skipping.
11. **Preserve provenance.**
    * Every selected rule contributes a `MatchedRule` entry with `rule_id`, `classification_status`, `confidence`, `exactness`, and `source_url`.

## Product classification

Providers may classify products into:

* `calculable` — a calculable fee rule exists.
* `included` / `free` / `waived` — no fee is charged.
* `custom_pricing` — requires negotiation; not calculable from public data.
* `unsupported` — known product but unsupported fee shape.
* `non_calculable` — known product with no public rate.

## Condition context construction

For Stripe, the request is flattened into a condition context map that includes:

* `account_country`
* `customer_country`
* `amount_currency`
* `transaction_amount`
* `presentment_currency`
* `settlement_currency`
* `product_id`, `variant_id`
* `payment_method`, `payment_method_variant`
* `channel`, `pricing_plan`, `pricing_tier`
* `payer`, `unit`
* `currency_conversion_required`
* `recurring`, `billing_type`
* `transaction_region`, `cross_border`
* `integration_type`, `product_feature`, `contract_length`, `feature_enabled`, `dispute_state`
* `success` (defaults to `true`)
* `card_*` fields flattened from `transaction.card`
* `settlement_timing` from `transaction.settlement.timing`
* `bank_account_validation` and `bank_transfer_type` from `transaction.bank`
* Any additional key/value pairs from `transaction.context` that do not conflict with typed fields

For PayPal, the context includes `account_country`, `customer_country`, `amount_currency`, `transaction_amount`, `product_id`, `variant_id`, `payment_method`, `transaction_region`, `payer_region`, `surcharge_region`, and all other transaction fields.

## Conflicting context

If `transaction.context` provides a value that conflicts with a typed transaction field, raise `QuoteNotAvailable` with the contradictory field path.
