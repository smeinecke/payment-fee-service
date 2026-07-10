from __future__ import annotations

from typing import Protocol

from payment_fee_service.domain.models import CapabilityInfo, MarketInfo, QuoteRequest
from payment_fee_service.domain.rules import CompiledFeePlan


class FeeProvider(Protocol):
    provider_id: str

    def compile_rules(self, request: QuoteRequest) -> CompiledFeePlan: ...

    def markets(self) -> list[MarketInfo]: ...

    def capabilities(self, account_country: str) -> CapabilityInfo: ...
