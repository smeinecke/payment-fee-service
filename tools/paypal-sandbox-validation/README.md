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

* `smoke` - One domestic and one cross-border case for the first merchant
  country (default: DE).
* `de-pilot` - A broader DE-centric subset.
* `full` - The full merchant/buyer/amount matrix. Use only after confirming the
  harness works for `smoke` and `de-pilot`.

## Safety

* Only `https://api-m.sandbox.paypal.com` and
  `https://www.sandbox.paypal.com` are allowed.
* The input CSV is treated as a secret and never copied into the repository.
* OAuth tokens are kept in memory only.
* Screenshots may be written to `artifacts/paypal-sandbox/<run-id>/screenshots/`
  on failures; they are excluded from version control.
* Run the full matrix only after reviewing smoke and pilot results.

## Known Limitations

* Business accounts whose `client_id` and `secret` are identical are treated
  as invalid credentials. `validate-config` reports them and `run`/`probe` skip
  them so that valid accounts (e.g. DE in the sample CSV) can still be used.
* Some PayPal Sandbox business accounts may fail OAuth or checkout with a
  `COMPLIANCE_VIOLATION` error. The harness records these as
  `account_configuration_difference` and continues when
  `--continue-after-mismatch` is used.
* The `payment-fee` library does not provide a `paypal_checkout` schedule for
  every country. In those cases the harness reports
  `account_capability_unavailable`.
