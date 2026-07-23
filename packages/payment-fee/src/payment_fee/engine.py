from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from payment_fee.calculator import FeeCalculator
from payment_fee.data_source import (
    DEFAULT_PAYPAL_URL,
    DEFAULT_STRIPE_URL,
    JsonDataSource,
    _load_documents,
    _location_from_string,
)
from payment_fee.errors import UnknownProvider
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
    def from_remote(
        cls,
        paypal: str | None = None,
        stripe: str | None = None,
        *,
        paypal_data_ref: str | None = None,
        stripe_data_ref: str | None = None,
        cache_dir: str | Path | None = None,
        ttl_seconds: float = 86400.0,
        auto_refresh: bool = True,
        validate: bool = False,
    ) -> PaymentFeeEngine:
        """Build an engine from remote data sources with TTL caching.

        ``paypal`` and ``stripe`` are base URLs. They may contain ``{data_ref}``,
        which is replaced by the corresponding ``*_data_ref`` argument or the
        pinned revision from ``contracts/data-revisions.json`` (falling back to
        ``main``).

        ``cache_dir`` defaults to ``~/.cache/payment-fee`` (or the system temp
        directory). ``ttl_seconds`` defaults to 24 hours.
        """
        if paypal is None and stripe is None:
            paypal = DEFAULT_PAYPAL_URL
            stripe = DEFAULT_STRIPE_URL

        registry = ProviderRegistry()
        if paypal:
            location = _location_from_string("paypal", paypal, paypal_data_ref)
            source = JsonDataSource(
                location,
                cache_dir=Path(cache_dir) if cache_dir else None,
                ttl_seconds=ttl_seconds,
                auto_refresh=auto_refresh,
            )
            docs = _load_documents(source, validate)
            registry.register(
                PayPalProvider.from_documents(
                    core=docs["core"],
                    index=docs["index"],
                    data_ref=location.data_ref,
                    validate_schema=validate,
                )
            )
        if stripe:
            location = _location_from_string("stripe", stripe, stripe_data_ref)
            source = JsonDataSource(
                location,
                cache_dir=Path(cache_dir) if cache_dir else None,
                ttl_seconds=ttl_seconds,
                auto_refresh=auto_refresh,
            )
            docs = _load_documents(source, validate)
            registry.register(
                StripeProvider.from_documents(
                    core=docs["core"],
                    index=docs["index"],
                    data_ref=location.data_ref,
                    validate_schema=validate,
                )
            )
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
                schemas = paypal.get("schemas")
                registry.register(
                    PayPalProvider.from_documents(
                        core=core,
                        index=index,
                        schemas=schemas,
                        data_ref="documents",
                        validate_schema=validate,
                    )
                )
        if stripe:
            if isinstance(stripe, StripeProvider):
                registry.register(stripe)
            else:
                core = stripe.get("core") or stripe
                index = stripe.get("index")
                schemas = stripe.get("schemas")
                registry.register(
                    StripeProvider.from_documents(
                        core=core,
                        index=index,
                        schemas=schemas,
                        data_ref="documents",
                        validate_schema=validate,
                    )
                )
        return cls(registry)

    def quote(self, request: BaseQuoteRequest | dict[str, Any]) -> QuoteResponse:
        if isinstance(request, dict):
            provider_id = request.get("provider")
            if provider_id and provider_id not in self._registry.providers():
                raise UnknownProvider(provider_id)
            try:
                request = _QUOTE_REQUEST_ADAPTER.validate_python(request)
            except ValidationError as exc:
                provider_id = request.get("provider") if isinstance(request, dict) else None
                if provider_id:
                    raise UnknownProvider(provider_id) from exc
                raise
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
