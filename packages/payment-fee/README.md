# payment-fee

Reusable Python library for estimating public standard payment processing fees
from structured provider datasets.

## Usage

```python
from payment_fee.engine import PaymentFeeEngine

# Load from local data directories
engine = PaymentFeeEngine.from_paths(
    paypal="/data/paypal-fee-data",
    stripe="/data/stripe-fee-data",
)

# Or download from the public data repositories with TTL caching
engine = PaymentFeeEngine.from_remote(
    cache_dir="/var/cache/payment-fee",  # defaults to ~/.cache/payment-fee
    ttl_seconds=24 * 60 * 60,            # default
    auto_refresh=True,                   # default
)

# Force a single-file refresh via JsonDataSource.refresh("json/core-fees.json")
```
