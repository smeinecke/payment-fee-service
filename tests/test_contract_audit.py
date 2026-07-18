from __future__ import annotations

from pathlib import Path

import pytest
from payment_fee import PaymentFeeEngine
from payment_fee.audit import audit_contract

PAYPAL_DATA = Path(__file__).parent.parent.parent / "paypal-fee-data"
STRIPE_DATA = Path(__file__).parent.parent.parent / "stripe-fee-data"


@pytest.mark.skipif(
    not (PAYPAL_DATA / "json/core-fees.json").exists() or not (STRIPE_DATA / "json/core-fees.json").exists(),
    reason="provider fee data not available",
)
def test_contract_audit_passes_for_current_data() -> None:
    engine = PaymentFeeEngine.from_paths(
        paypal=str(PAYPAL_DATA),
        stripe=str(STRIPE_DATA),
    )
    result = audit_contract(engine)

    assert result.paypal_calculable_rules_skipped == 0
    assert result.stripe_calculable_rules_skipped == 0
    assert result.unknown_fields == 0
    assert result.unknown_condition_operators == 0
    assert result.unresolved_schedule_references == 0
    assert result.unsupported_fee_components == 0
    assert result.paypal_calculable_rules_total > 0
    assert result.stripe_calculable_rules_total > 0

    print(
        "PayPal:",
        result.paypal_calculable_rules_total,
        result.paypal_calculable_rules_parsed,
        result.paypal_calculable_rules_skipped,
    )
    print(
        "Stripe:",
        result.stripe_calculable_rules_total,
        result.stripe_calculable_rules_parsed,
        result.stripe_calculable_rules_skipped,
    )
    for failure in result.failures[:20]:
        print(failure)
