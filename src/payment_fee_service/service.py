from __future__ import annotations

from payment_fee_service.domain.calculator import FeeCalculator
from payment_fee_service.domain.models import QuoteRequest, QuoteResponse
from payment_fee_service.providers.registry import ProviderRegistry


class QuoteService:
    def __init__(self, registry: ProviderRegistry, calculator: FeeCalculator | None = None) -> None:
        self.registry = registry
        self.calculator = calculator or FeeCalculator()

    def calculate(self, request: QuoteRequest) -> QuoteResponse:
        provider = self.registry.get(request.provider)
        plan = provider.compile_rules(request)
        return self.calculator.calculate(request.amount, plan)
