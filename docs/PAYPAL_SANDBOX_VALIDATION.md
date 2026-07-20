# PayPal Sandbox validation methodology

This document describes how the repository validates PayPal Sandbox fee
behavior while keeping Sandbox evidence separate from production public-rate
data.

## 1. Production public fee source

Production estimates use the public PayPal merchant fee pages consumed by
`smeinecke/paypal-fee-crawler` and published in `smeinecke/paypal-fee-data`.
The `payment-fee` libraries load those snapshots and apply the same rule
selection and decimal arithmetic across Python, PHP, and TypeScript.

These public pages are the canonical source for production-oriented fee
estimates. They do not require authentication and do not expose account-specific
pricing.

## 2. Authenticated Sandbox profile pricing

A PayPal Sandbox Business account can display an environment-specific pricing
page after login:

```text
https://www.sandbox.paypal.com/merchantapps/businesstools/acceptpayments/checkout
```

The page is only accessible while authenticated as the Sandbox Business account
that owns it. It often shows two pricing cards:

* `When customers pay with PayPal` — the wallet rate.
* `When customers pay with credit or debit card` — the card rate, sometimes
  prefixed with `Starting at`.

Observed examples:

* DE Sandbox: wallet `1.90% + EUR 0.35`, card `Starting at 2.99% + EUR 0.39`.
* AU Sandbox: wallet `2.40% + AUD 0.30`, card `Starting at 1.75% + AUD 0.30`.

These values are stored as `sandbox_profile_pricing` evidence. They are
labeled by `environment: sandbox`, `pricing_source: sandbox_merchant_profile`,
and `representative_for_public_rates: false`.

## 3. Transaction evidence

`observed_transaction_pricing` comes from structured Sandbox transaction
evidence:

* PayPal Orders v2 capture responses.
* NVP `GetTransactionDetails`.
* Other harness output that records the gross amount, PayPal fee, net amount,
  and observed payer country.

A Sandbox account is internally consistent when the wallet formula from the
profile page matches the fees observed in transactions. That does **not** imply
the profile formula matches the production public tariff.

## 4. Qualification decisions

The qualification registry stores one entry per merchant country. Each entry
can hold:

* `status` — the overall merchant classification.
* `orders_v2_checkout` — status for the Orders-v2 checkout execution path.
* `manual_send_to_business` — status for the manual Send Money path.
* `sandbox_profile_pricing` — the profile evidence block.
* `representative_for_public_rates` — always `false` when profile pricing is
  the evidence source.

The status `sandbox_profile_pricing_confirmed` means the profile page itself
supplies the explanation for the observed Sandbox fees, not that the Sandbox
account is representative of production public rates.

## 5. Why Sandbox results must not rewrite production datasets

Sandbox Business accounts can display rates that differ from the public
production pages. Writing Sandbox profile rates to `paypal-fee-data` would
contaminate the production-oriented dataset with account-specific and
environment-specific values. The harness therefore keeps three separate
scopes:

* `production_public_pricing`
* `sandbox_profile_pricing`
* `observed_transaction_pricing`

A match between `sandbox_profile_pricing` and `observed_transaction_pricing`
validates the harness and the Sandbox account. It does not prove the Sandbox
rate is a universal production rate.

## 6. DE findings

For the tested DE Sandbox Business account:

| Evidence source | Formula |
| --- | --- |
| Sandbox profile wallet | `1.90% + EUR 0.35` |
| Observed manual-send transactions | `1.90% + EUR 0.35` |
| Sandbox profile card | `Starting at 2.99% + EUR 0.39` |
| Production public wallet | `2.49% + EUR 0.35` |

`profile_matches_transactions` is confirmed for the wallet formula.
`representative_for_public_rates` is `false`.

## 7. AU findings

For the tested AU Sandbox Business account:

| Evidence source | Formula |
| --- | --- |
| Sandbox profile wallet | `2.40% + AUD 0.30` |
| Observed domestic checkout transactions | `2.40% + AUD 0.30` |
| Sandbox profile card | `Starting at 1.75% + AUD 0.30` |
| AU → US buyer at AUD 10.00 | `2.40% + 1.00pp + AUD 0.30 = AUD 0.64` |

The international surcharge is confirmed only for the tested `AU merchant ←
US buyer, AUD 10.00` case. It has not been generalized to every payer region.
`representative_for_public_rates` is `false`.

## 8. Orders-v2 compliance limitations

Some Sandbox Business accounts cannot complete the Orders-v2 checkout flow due
to `COMPLIANCE_VIOLATION` errors. Those cases are recorded as
`sandbox_checkout_limitation` or `account_configuration_blocked`, not as
pricing mismatches. The manual Send Money path and the authenticated profile
page remain available as alternative Sandbox evidence sources.

## 9. Evidence retention and sanitization

Profile pricing evidence is secret-free by construction. The stored record
contains only:

* `provider`, `environment`, `merchant_country`
* `evidence_type`, `pricing_source`, `profile_page`
* `wallet` and `card` percentage and fixed fee values
* optional `international_surcharge` metadata
* optional `screenshot_sha256`
* `contains_account_identifiers: false`

It never contains:

* passwords, client IDs, secrets, or NVP credentials
* cookies, session tokens, or storage state
* account identifiers, email addresses, or transaction IDs
* full screenshot files

Screenshots supplied by users must be kept in ignored artifact paths and must
not be committed.

## 10. Reproduction commands

Validate configuration:

```bash
paypal-sandbox-validation validate-config
```

Record manually observed profile pricing:

```bash
paypal-sandbox-validation record-profile-pricing \
  --merchant DE \
  --wallet-percentage 1.90 \
  --wallet-fixed 0.35 \
  --wallet-currency EUR \
  --card-percentage 2.99 \
  --card-fixed 0.39 \
  --card-currency EUR \
  --card-qualifier starting_at
```

Optionally inspect the authenticated profile page (requires the confidential
account CSV and opens a browser; no payment is executed):

```bash
paypal-sandbox-validation inspect-profile-pricing --merchant DE --headed
```

Run the non-live test suite:

```bash
make validate
make test-unit
make test-conformance
make audit-contract
```
