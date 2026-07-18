from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from payment_fee.calculator import FeeCalculator
from payment_fee.models import (
    BaseQuoteRequest,
    CapabilityInfo,
    MarketInfo,
    PayPalQuoteRequest,
    ProviderInfo,
    QuoteRequest,
    QuoteResponse,
    QuoteSchema,
    StripeQuoteRequest,
)
from payment_fee.providers import PayPalProvider, StripeProvider
from payment_fee.registry import ProviderRegistry
from payment_fee.rules import CompiledFeePlan

_QUOTE_REQUEST_ADAPTER: TypeAdapter[BaseQuoteRequest] = TypeAdapter(QuoteRequest)


class PaymentFeeEngine:
    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry
        self._calculator = FeeCalculator()

    @classmethod
    def from_paths(
        cls,
        paypal: str | None = None,
        stripe: str | None = None,
        validate: bool = False,
    ) -> PaymentFeeEngine:
        registry = ProviderRegistry()
        if paypal:
            registry.register(PayPalProvider.from_paths(paypal, data_ref="local", validate_schema=validate))
        if stripe:
            registry.register(StripeProvider.from_paths(stripe, data_ref="local", validate_schema=validate))
        return cls(registry)

    @classmethod
    def from_documents(
        cls,
        paypal: dict[str, Any] | PayPalProvider | None = None,
        stripe: dict[str, Any] | StripeProvider | None = None,
        validate: bool = False,
    ) -> PaymentFeeEngine:
        registry = ProviderRegistry()
        if paypal:
            if isinstance(paypal, PayPalProvider):
                registry.register(paypal)
            else:
                core = paypal.get("core") or paypal
                index = paypal.get("index")
                registry.register(
                    PayPalProvider.from_documents(
                        core=core,
                        index=index,
                        data_ref="documents",
                    )
                )
        if stripe:
            if isinstance(stripe, StripeProvider):
                registry.register(stripe)
            else:
                core = stripe.get("core") or stripe
                index = stripe.get("index")
                payment_methods = stripe.get("payment_methods")
                registry.register(
                    StripeProvider.from_documents(
                        core=core,
                        index=index,
                        payment_methods=payment_methods,
                        data_ref="documents",
                    )
                )
        return cls(registry)

    def quote(self, request: BaseQuoteRequest | dict[str, Any]) -> QuoteResponse:
        if isinstance(request, dict):
            request = _QUOTE_REQUEST_ADAPTER.validate_python(request)
        provider = self._registry.get(request.provider)
        plan = provider.compile_rules(request)
        return self._calculator.calculate(request.amount, plan)

    def providers(self) -> list[str]:
        return self._registry.providers()

    def markets(self, provider: str) -> list[MarketInfo]:
        return self._registry.get(provider).markets()

    def capabilities(self, provider: str, market: str) -> CapabilityInfo:
        return self._registry.get(provider).capabilities(market)

    def quote_schema(self, provider: str, market: str) -> QuoteSchema:
        return self._registry.get(provider).quote_schema(market)

    def data_status(self) -> list[ProviderInfo]:
        return self._registry.infos()

    def _compile_rules(
        self,
        provider_id: str,
        request: PayPalQuoteRequest | StripeQuoteRequest,
    ) -> CompiledFeePlan:
        return self._registry.get(provider_id).compile_rules(request)
