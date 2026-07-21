from __future__ import annotations

from typing import Any, Protocol

from payment_fee.errors import QuoteNotAvailable, UnsupportedFeeShape
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema
from payment_fee.rules import CompiledFeePlan


def _check_schema_version(model: Any, supported: set[int], provider_name: str) -> None:
    if model.schema_version not in supported:
        raise UnsupportedFeeShape(
            f"Unsupported {provider_name} schema version: {model.schema_version}",
            supported=sorted(supported),
        )


def _merge_context_overrides(context: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Merge free-form transaction context into a typed context, raising on contradiction."""
    for key, value in overrides.items():
        if key in context:
            if context[key] is None:
                context[key] = value
            elif value != context[key]:
                raise QuoteNotAvailable(
                    "Contradictory duplicate value in transaction context.",
                    field=key,
                    typed_value=context[key],
                    context_value=value,
                )
        else:
            context[key] = value


class FeeProvider(Protocol):
    provider_id: str

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan: ...

    def markets(self) -> list[MarketInfo]: ...

    def capabilities(self, account_country: str) -> CapabilityInfo: ...

    def quote_schema(self, account_country: str) -> QuoteSchema: ...

    def data_status(self) -> dict[str, Any]: ...
