# Quote Hot Path Benchmark

This document records a reproducible, before-and-after benchmark of the Python
quote hot path for the `payment-fee-service`.  It is intended to measure the
impact of the Phase 8 Pydantic static-model caching work and related provider
optimizations.

## Methodology

All runs use a single, representative Stripe US request that exercises the
currently largest provider market (149 rules).  The request is the same across
runs; only the checkout of `packages/payment-fee` changes.

* **Provider:** `stripe`
* **Market:** `US`
* **Rules in market:** 149
* **Amount:** `100.00 USD`
* **Request shape:**

  ```json
  {
    "provider": "stripe",
    "amount": {"value": "100.00", "currency": "USD"},
    "account_country": "US",
    "transaction": {
      "product_id": "payments",
      "variant_id": "online_domestic_cards",
      "payment_method": "card",
      "channel": "online",
      "card": {"origin": "domestic", "region": "domestic"}
    }
  }
  ```

* **Warm-up requests:** 500
* **Timed requests:** 5,000
* **Python:** 3.13.5
* **Data revisions:**
  * `stripe-fee-data`: `f9a36533f3bcb70d58505ceeae63ec8c119c0e6b`
  * `paypal-fee-data`: `5f9d0224bc529b53fb9d20852fce57e897666627`

## Command

```bash
python scripts/benchmark_quote_hot_path.py \
  --data-dir stripe-fee-data \
  --account-country US \
  --amount 100.00 \
  --currency USD \
  --warmup 500 \
  --requests 5000
```

The baseline run uses the same script in a clean worktree checked out at the
pre-optimization commit:

```bash
git worktree add /tmp/payment-fee-baseline 4b05590
ln -s $(pwd)/stripe-fee-data /tmp/payment-fee-baseline/stripe-fee-data
PYTHONPATH=/tmp/payment-fee-baseline/packages/payment-fee/src \
  .venv/bin/python /tmp/payment-fee-baseline/scripts/benchmark_quote_hot_path.py \
  --data-dir stripe-fee-data --account-country US --amount 100.00 --currency USD \
  --warmup 500 --requests 5000
```

## Results

The table below shows the average of three independent runs for each checkout.

| Checkout | Commit | Median (ms) | p95 (ms) | Mean (ms) | RPS |
| --- | --- | --- | --- | --- | --- |
| Baseline (origin/main) | `4b05590` | 0.499 | 0.550 | 0.507 | 1,973 |
| Optimized (working tree) | current | 0.488 | 0.533 | 0.495 | 2,022 |

**Change:** approximately **+2.5% throughput** and **~2% lower median latency**.

## Interpretation

The optimized checkout avoids reconstructing `ExecutableFeeRule` and
`CompiledFeePlan` Pydantic models from scratch on every request by keeping
statically-validated model templates in the provider and applying only the
per-request financial fields with `model_copy(update=...)`.  The hot path is
dominated by rule matching and `Decimal` fee-component resolution, so the
end-to-end latency improvement is modest but consistent.  The benchmark confirms
that the caching change does not introduce a measurable regression and provides a
small, reproducible throughput gain on the most heavily-populated Stripe market.
