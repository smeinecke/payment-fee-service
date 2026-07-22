from decimal import Decimal

import pytest
from payment_fee.providers.base import _values_equal


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (True, True, True),
        (False, False, True),
        (True, False, False),
        (False, True, False),
        (True, 1, False),
        (False, 0, False),
        (1, True, False),
        (0, False, False),
        (True, "true", False),
        (False, "false", False),
        ("true", True, False),
        ("false", False, False),
    ],
)
def test_boolean_equality_is_strict(left, right, expected) -> None:
    """Booleans only match booleans of the same value."""
    assert _values_equal(left, right) is expected


def test_string_equality_is_casefolded() -> None:
    assert _values_equal("USD", "usd") is True
    assert _values_equal("Domestic", "DOMESTIC") is True
    assert _values_equal("us", "eu") is False


def test_numeric_equality_coerces_types() -> None:
    assert _values_equal("2.90", Decimal("2.9")) is True
    assert _values_equal(10, Decimal("10.0")) is True
    assert _values_equal("1.1", Decimal("1.10")) is True
    assert _values_equal("1", "2") is False
