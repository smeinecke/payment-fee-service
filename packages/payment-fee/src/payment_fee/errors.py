from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaymentFeeError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class UnknownProvider(PaymentFeeError):
    def __init__(self, provider: str, **details: Any) -> None:
        super().__init__(
            code="UNKNOWN_PROVIDER",
            message=f"Unknown provider: {provider}",
            details={"provider": provider, **details},
        )


class UnknownMarket(PaymentFeeError):
    def __init__(self, provider: str, market: str, **details: Any) -> None:
        super().__init__(
            code="UNKNOWN_MARKET",
            message=f"Provider {provider} has no published market {market}.",
            details={"provider": provider, "market": market, **details},
        )


class ProviderDataUnavailable(PaymentFeeError):
    def __init__(self, provider: str, reason: str, **details: Any) -> None:
        super().__init__(
            code="PROVIDER_DATA_UNAVAILABLE",
            message=f"Validated data for {provider} is unavailable.",
            details={"provider": provider, "reason": reason, **details},
        )


class QuoteNotAvailable(PaymentFeeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="QUOTE_NOT_AVAILABLE",
            message=message,
            details=details,
        )


class InsufficientTransactionContext(PaymentFeeError):
    def __init__(self, missing_fields: list[str], **details: Any) -> None:
        super().__init__(
            code="INSUFFICIENT_TRANSACTION_CONTEXT",
            message="Additional transaction context is required to select an applicable fee rule.",
            details={"missing_fields": sorted(set(missing_fields)), **details},
        )


class AmbiguousFeeRules(PaymentFeeError):
    def __init__(self, rule_ids: list[str], **details: Any) -> None:
        super().__init__(
            code="AMBIGUOUS_FEE_RULES",
            message="Multiple equally specific fee rules matched with different fee values.",
            details={"candidate_rule_ids": sorted(set(rule_ids)), **details},
        )


class UnsupportedFeeShape(PaymentFeeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="UNSUPPORTED_FEE_SHAPE",
            message=message,
            details=details,
        )


class CurrencyMismatch(PaymentFeeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="CURRENCY_MISMATCH",
            message=message,
            details=details,
        )


class DatasetValidationError(PaymentFeeError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="DATASET_VALIDATION_ERROR",
            message=message,
            details=details,
        )
