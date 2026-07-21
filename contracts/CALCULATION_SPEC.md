# Calculation Specification

## Calculation order

For each selected rule:

```text
base percentage
+ direct fixed amount
+ schedule fixed amount
+ additive percentage
+ additive fixed amount
apply minimum
apply maximum
round in fee currency
calculate net amount
```

The final `processing_fee` is the rounded sum of all component amounts. The `net_amount` is `amount - processing_fee`, rounded to the currency quantum.

## Fee components

A calculable rule may produce one `FeeComponent`. The component carries:

* `type` — e.g. `percentage`, `fixed_amount`, `fixed_fee_schedule`, `international_surcharge_schedule`, `maximum_fee_schedule`, `fixed_surcharge`, `percentage_surcharge`, `minimum_fee`, `included`.
* `label` — human-readable description.
* `amount` — calculated and rounded fee amount as a decimal string.
* `currency` — fee currency code.
* `rate_percentage` — optional percentage rate used to calculate the amount.
* `fixed_amount` — optional fixed amount used to calculate the amount.
* `minimum_applied` / `maximum_applied` — flags indicating whether a cap was applied.
* `payer` — who pays the fee, if known.
* `unit` — e.g. `per_transaction`, `per_authorization`, `per_attempt`, `per_dispute`, `monthly`.
* `source_rule_id` — the provider rule ID.

## Percentage rules

* If `basis_points` is provided: `amount * basis_points / 10000`.
* If `percentage` is provided: `amount * percentage / 100`.
* `rate_percentage` is recorded on the component as the equivalent percentage string.

## Fixed rules

* `fixed_amount` is added to the raw total as-is.
* If the fixed amount currency differs from the transaction currency, raise `CurrencyMismatch`.

## PayPal schedule fixed amounts

A PayPal rule may reference:

* `fixed_fee_schedule` — a schedule ID mapped to `currency -> amount`.
* `international_surcharge_schedule` — a schedule of `payer_region -> percentage_points`.
* `maximum_fee_schedule` — a schedule ID mapped to `currency -> max amount`.

Schedule IDs must be resolved through the explicit schedule registry. Prefix heuristics are not allowed. Missing schedule references raise `QuoteNotAvailable` and count toward `unresolved_schedule_references` in an audit.

## Minimum and maximum fees

* If `minimum_amount` is set and the raw amount is less than the minimum, the amount becomes the minimum and `minimum_applied` is `true`.
* If `maximum_amount` is set and the raw amount is greater than the maximum, the amount becomes the maximum and `maximum_applied` is `true`.
* Minimum and maximum are applied after percentage and fixed amounts.

## Included / free / waived rules

Rules with behavior `free`, `included`, or `waived` produce a component with amount `0` and type `included`. They do not contribute to the `processing_fee` total.

## Exact, estimated, and range rates

* `exact_for_public_rate` — exact public published rate.
* `estimated` — derived or inferred value.
* `range` — the provider publishes a range (e.g. starting-at, up-to). The response status is `range`.
* `from` / `up_to` — exactness markers that cause status `range`.
* `starting_at` or `up_to` values are not treated as exact quotes.

No implementation may invent exact quotes from non-exact public rates.

## Unsupported fee shapes

If a rule uses a fee shape that is not supported by the calculator (e.g. recurring/monthly fees with unsupported unit semantics, per-authorization without a clear amount, payout-volume percentages without a volume base, or unsupported component types), raise `UnsupportedFeeShape`.

## Recurring and billing

Recurring fees (`unit = monthly`) and per-event fees (`per_authorization`, `per_attempt`, `per_dispute`) are preserved in the `unit` field. If the fee shape cannot be reduced to a single transaction fee, the status is `range` and `UnsupportedFeeShape` is raised where appropriate.

## Currency conversion surcharge

Stripe currency conversion surcharges are additive percentage components applied when `currency_conversion_required` is true.

## Cross-border surcharge

Stripe cross-border surcharges are additive components applied when `cross_border` is true or when card/customer region indicates an international transaction.

## Net amount

`net_amount = round(amount - processing_fee)`.

If `processing_fee > amount`, `net_amount` may be negative. Negative values serialize with a leading `-`. Negative zero serializes as `0`.
