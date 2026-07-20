# PayPal Sandbox Validation Harness

A standalone CLI tool inside the `payment-fee-service` workspace that reconciles
PayPal Sandbox transaction fees against the local `payment-fee` library.

## Setup

The package is a member of the UV workspace. From the repository root:

```bash
uv sync
make paypal-sandbox-install-playwright
```

Place the confidential account CSV outside the repository and export its path:

```bash
export PAYPAL_SANDBOX_ACCOUNTS_CSV=/secure/path/paypal-sandbox-accounts.csv
```

## Usage

```bash
paypal-sandbox-validation validate-config
paypal-sandbox-validation probe
paypal-sandbox-validation plan --profile smoke
paypal-sandbox-validation run --profile smoke --continue-after-mismatch
paypal-sandbox-validation reconcile --run-id <run-id>
paypal-sandbox-validation report --run-id <run-id>
```

Record manually observed Sandbox Business profile pricing (no payment executed):

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

Optionally inspect the authenticated profile page automatically (read-only, no
payment):

```bash
paypal-sandbox-validation inspect-profile-pricing --merchant DE --headed
```

Root Make targets are also provided:

```bash
make paypal-sandbox-validate-config
make paypal-sandbox-probe
make paypal-sandbox-plan
make paypal-sandbox-smoke
make paypal-sandbox-report
```

Run `make paypal-sandbox-report PAYPAL_SANDBOX_VALIDATION_RUN=<run-id>` to
regenerate a report for a specific run.

## Profiles

* `smoke` - One domestic and one cross-border case using the US merchant
  (USD domestic US and cross-border CA).
* `de-pilot` - A broader DE-centric subset.
* `full` - The full merchant/buyer/amount matrix. Use only after confirming the
  harness works for `smoke` and `de-pilot`.

## Sandbox profile pricing is not production pricing

The authenticated Sandbox Business account pricing page is:

```text
https://www.sandbox.paypal.com/merchantapps/businesstools/acceptpayments/checkout
```

Log in with the specific Business Sandbox account before opening the page. The
URL redirects to sign-in when no authenticated Sandbox session exists.

* The Sandbox Business account may display rates that differ from public
  production rates.
* Rates displayed there explain Sandbox transaction fees but must not be written
  to `paypal-fee-data`.
* Public production fee pages remain the source for production estimates.
* `sandbox_profile_pricing` and `production_public_pricing` are separate
  evidence scopes.
* A Sandbox match validates the harness and the Sandbox account behavior, not
  necessarily the production public tariff.
* Card values marked `Starting at` are lower bounds/offer indications and must
  not be modeled as exact universal rules.
* Screenshots may contain account-dependent data and must remain in ignored
  local artifact paths.
* Never commit credentials, account identifiers, transaction IDs, cookies or
  session data.

Current observations from tested Sandbox Business accounts:

| Market | Sandbox profile wallet rate | Transactionally verified | Production representative |
| --- | --- | --- | --- |
| DE | 1.90% + EUR 0.35 | Yes | No |
| AU | 2.40% + AUD 0.30 | Yes | No |

The AU merchant → US buyer case at AUD 10.00 confirmed the account base plus a
1.00 percentage-point international surcharge. This has not been generalized to
every payer region.

See [docs/PAYPAL_SANDBOX_VALIDATION.md](../../docs/PAYPAL_SANDBOX_VALIDATION.md)
for the full methodology and the distinction between production public pricing,
Sandbox profile pricing and observed transaction pricing.

## Safety

* Only `https://api-m.sandbox.paypal.com` and
  `https://www.sandbox.paypal.com` are allowed.
* The input CSV is treated as a secret and never copied into the repository.
* OAuth tokens are kept in memory only.
* `inspect-profile-pricing` creates a fresh Playwright context, never persists
  cookies or storage state, and navigates only to the allow-listed Sandbox
  profile page.
* Screenshots may be written to `artifacts/paypal-sandbox/<run-id>/screenshots/`
  on failures; they are excluded from version control.
* Run the full matrix only after reviewing smoke and pilot results.

## Known Limitations

* Business accounts whose `client_id` and `secret` are identical are treated
  as invalid credentials. `validate-config` reports them and `run`/`probe` skip
  them so that valid accounts (e.g. DE in the sample CSV) can still be used.
* Some PayPal Sandbox business accounts may fail OAuth or checkout with a
  `COMPLIANCE_VIOLATION` error. The harness records these as
  `sandbox_checkout_limitation` or `account_configuration_blocked` and
  continues when `--continue-after-mismatch` is used.
* The `payment-fee` library does not provide a `paypal_checkout` schedule for
  every country. In those cases the harness reports
  `account_capability_unavailable`.
