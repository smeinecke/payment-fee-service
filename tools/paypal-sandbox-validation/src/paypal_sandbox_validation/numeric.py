from __future__ import annotations

from decimal import Decimal
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    """Parse a value into a Decimal, returning None for invalid/missing values."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
