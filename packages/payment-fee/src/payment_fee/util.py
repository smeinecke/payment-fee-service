from __future__ import annotations

from typing import Any, overload


def _as_list(value: Any) -> list[Any]:
    """Return the value as-is if it is a list, otherwise wrap it in a list."""
    if isinstance(value, list):
        return value
    return [value]


def _normalize_confidence(value: Any) -> Any:
    """Convert float whole numbers to int for cleaner serialization."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@overload
def normalize_currency(value: str) -> str: ...


@overload
def normalize_currency(value: None) -> None: ...


def normalize_currency(value: str | None) -> str | None:
    """Normalize a currency or country code to uppercase, preserving None."""
    return value.upper() if value else None
