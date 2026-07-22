#!/usr/bin/env python3
"""Benchmark the Python quote hot path for a Stripe market with many rules.

This script is intentionally standalone and deterministic: it pins the provider
and market, runs a warm-up phase, then times a fixed number of sequential quote
requests and reports median/p95 latency and throughput.  It is designed to be
run against two checkouts (pre/post optimization) using the same data directory.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark payment-fee quote hot path.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "stripe-fee-data",
        help="Path to the stripe-fee-data checkout to load.",
    )
    parser.add_argument("--account-country", default="US", help="Market to benchmark.")
    parser.add_argument("--requests", type=int, default=1000, help="Number of quote requests to time.")
    parser.add_argument("--warmup", type=int, default=100, help="Number of warm-up requests.")
    parser.add_argument("--amount", default="100.00", help="Transaction amount value.")
    parser.add_argument("--currency", default="USD", help="Transaction currency.")
    return parser.parse_args()


def build_request(account_country: str, amount: str, currency: str) -> dict:
    return {
        "provider": "stripe",
        "amount": {"value": amount, "currency": currency},
        "account_country": account_country,
        "transaction": {
            "product_id": "payments",
            "variant_id": "online_domestic_cards",
            "payment_method": "card",
            "channel": "online",
            "card": {"origin": "domestic", "region": "domestic"},
        },
    }


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct / 100.0
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_values) else f
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def main() -> int:
    args = parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "payment-fee" / "src"))
    from payment_fee import PaymentFeeEngine

    data_dir = args.data_dir
    if not (data_dir / "json" / "core-fees.json").exists():
        print(f"Stripe data not found at {data_dir}", file=sys.stderr)
        return 1

    engine = PaymentFeeEngine.from_paths(stripe=str(data_dir))
    provider = engine._registry._providers["stripe"]
    market = provider._markets[args.account_country.upper()]
    rule_count = len(market.rules)

    request = build_request(args.account_country, args.amount, args.currency)

    # Warm-up: ensure caches are hot.
    for _ in range(args.warmup):
        engine.quote(request)

    latencies: list[float] = []
    for _ in range(args.requests):
        start = time.perf_counter()
        engine.quote(request)
        latencies.append(time.perf_counter() - start)

    latencies.sort()
    median = percentile(latencies, 50.0)
    p95 = percentile(latencies, 95.0)
    total = sum(latencies)
    mean = statistics.mean(latencies)
    rps = args.requests / total if total > 0 else 0.0

    print("provider: stripe")
    print(f"market: {args.account_country}")
    print(f"rules: {rule_count}")
    print(f"requests: {args.requests}")
    print(f"warmup: {args.warmup}")
    print(f"python: {sys.version.split()[0]}")
    print(f"data_dir: {data_dir}")
    print(f"median_latency_ms: {median * 1000:.3f}")
    print(f"p95_latency_ms: {p95 * 1000:.3f}")
    print(f"mean_latency_ms: {mean * 1000:.3f}")
    print(f"total_time_s: {total:.3f}")
    print(f"requests_per_second: {rps:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
