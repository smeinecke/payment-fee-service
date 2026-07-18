from decimal import Decimal

from payment_fee.calculator import FeeCalculator
from payment_fee.models import Money
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule


def test_calculates_percentage_fixed_minimum_and_rounding() -> None:
    plan = CompiledFeePlan(
        provider="test",
        market="DE",
        currency="EUR",
        rules=[
            ExecutableFeeRule(
                rule_id="rule-1",
                label="Processing fee",
                percentage=Decimal("1.5"),
                fixed_amount=Decimal("0.25"),
                fixed_currency="EUR",
            )
        ],
    )
    quote = FeeCalculator().calculate(Money(value=Decimal("100"), currency="EUR"), plan)
    assert quote.processing_fee.value == Decimal("1.75")
    assert quote.net_amount.value == Decimal("98.25")


def test_zero_decimal_currency_rounding() -> None:
    plan = CompiledFeePlan(
        provider="test",
        market="JP",
        currency="JPY",
        rules=[ExecutableFeeRule(rule_id="rule-1", label="Fee", percentage=Decimal("3.4"))],
    )
    quote = FeeCalculator().calculate(Money(value=Decimal("101"), currency="JPY"), plan)
    assert quote.processing_fee.value == Decimal("3")
