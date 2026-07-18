#!/usr/bin/env python3
"""Run the contract audit for the Python implementation.

When PHP and TypeScript providers are complete, this runner can be extended to
invoke each language's auditContract() method and compare counters.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from payment_fee import PaymentFeeEngine
from payment_fee.audit import audit_contract


def main() -> int:
    paypal = os.environ.get("PAYPAL_FEE_DATA")
    stripe = os.environ.get("STRIPE_FEE_DATA")
    if not paypal or not stripe:
        print("PAYPAL_FEE_DATA and STRIPE_FEE_DATA must be set.", file=sys.stderr)
        return 1

    engine = PaymentFeeEngine.from_paths(paypal=paypal, stripe=stripe)
    result = audit_contract(engine)
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
