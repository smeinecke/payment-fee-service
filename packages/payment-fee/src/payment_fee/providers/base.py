from __future__ import annotations

from typing import Any, Protocol

from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema
from payment_fee.rules import CompiledFeePlan


class FeeProvider(Protocol):
    provider_id: str

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan: ...

    def markets(self) -> list[MarketInfo]: ...

    def capabilities(self, account_country: str) -> CapabilityInfo: ...

    def quote_schema(self, account_country: str) -> QuoteSchema: ...

    def data_status(self) -> dict[str, Any]: ...
