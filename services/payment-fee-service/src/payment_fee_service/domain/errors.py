from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ServiceError(Exception):
    code: str
    message: str
    status_code: int = 422
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class UnknownProviderError(ServiceError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            code="UNKNOWN_PROVIDER",
            message=f"Unknown provider: {provider}",
            status_code=404,
            details={"provider": provider},
        )


class UnknownMarketError(ServiceError):
    def __init__(self, provider: str, market: str) -> None:
        super().__init__(
            code="UNKNOWN_MARKET",
            message=f"Provider {provider} has no published market {market}.",
            status_code=404,
            details={"provider": provider, "market": market},
        )


class QuoteUnavailableError(ServiceError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="QUOTE_NOT_AVAILABLE",
            message=message,
            status_code=422,
            details=details,
        )


class InsufficientContextError(ServiceError):
    def __init__(self, missing_fields: list[str], **details: Any) -> None:
        super().__init__(
            code="INSUFFICIENT_TRANSACTION_CONTEXT",
            message="Additional transaction context is required to select an applicable fee rule.",
            status_code=422,
            details={"missing_fields": sorted(set(missing_fields)), **details},
        )


class AmbiguousRulesError(ServiceError):
    def __init__(self, rule_ids: list[str]) -> None:
        super().__init__(
            code="AMBIGUOUS_FEE_RULES",
            message="Multiple equally specific fee rules matched with different fee values.",
            status_code=422,
            details={"candidate_rule_ids": sorted(rule_ids)},
        )


class DataUnavailableError(ServiceError):
    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(
            code="PROVIDER_DATA_UNAVAILABLE",
            message=f"Validated data for {provider} is unavailable.",
            status_code=503,
            details={"provider": provider, "reason": reason},
        )
