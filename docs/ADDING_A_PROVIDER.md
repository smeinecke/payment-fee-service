# Adding a Provider

A provider adapter lives in `packages/payment-fee/src/payment_fee/providers/<name>/`. It owns source loading, provider-specific request context, rule matching, and compilation into shared executable fee rules.

## Required work

1. Add a Pydantic request model with a literal `provider` discriminator.
2. Add the model to `QuoteRequest` in `packages/payment-fee/src/payment_fee/models.py`.
3. Add a transaction context model if needed.
4. Implement a provider module that loads and validates the published snapshot.
5. Implement `FeeProvider` (`compile_rules`, `markets`, `capabilities`, `quote_schema`, `data_status`).
6. Compile provider data into `CompiledFeePlan` and `ExecutableFeeRule` objects.
7. Export the provider from `packages/payment-fee/src/payment_fee/providers/__init__.py`.
8. Wire the provider into `PaymentFeeEngine.from_paths` and `from_documents` in `packages/payment-fee/src/payment_fee/engine.py`.
9. Add shared contract tests and provider-specific edge cases.

## Boundary

The shared calculator may understand only mathematical fee concepts:

- percentage
- basis points
- fixed amount and currency
- minimum and maximum amount
- additive/base behavior
- currency rounding

It must not understand provider concepts such as card regions, wallet types, transaction categories, or pricing page table names.

## Fail-closed rules

Reject a quote when:

- a required request dimension is missing
- source data is unclassified
- equally applicable rules conflict
- a fixed fee currency is incompatible
- a rule behavior or condition operator is unknown
- a referenced fee component is absent

Never silently choose the first rule or convert a missing component to zero.
