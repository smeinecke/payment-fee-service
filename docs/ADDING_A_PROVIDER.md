# Adding a Provider

A provider adapter owns source loading, provider-specific request context, rule matching, and compilation into shared executable fee rules.

## Required work

1. Add a request model with a literal `provider` discriminator.
2. Add the model to `QuoteRequest`.
3. Implement a repository that loads and validates the provider's published snapshot.
4. Implement `FeeProvider`.
5. Compile provider data into `CompiledFeePlan` and `ExecutableFeeRule` objects.
6. Register the provider in `bootstrap.build_registry`.
7. Add shared contract tests and provider-specific edge cases.

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
