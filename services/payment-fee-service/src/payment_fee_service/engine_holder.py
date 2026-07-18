from __future__ import annotations

import asyncio
import logging
from typing import Any

from payment_fee import PaymentFeeEngine
from payment_fee.errors import (
    InsufficientTransactionContext,
    PaymentFeeError,
    ProviderDataUnavailable,
    QuoteNotAvailable,
)
from payment_fee.models import CapabilityInfo, ProviderInfo

from payment_fee_service.bootstrap import build_engine
from payment_fee_service.settings import Settings

logger = logging.getLogger(__name__)


class EngineHolder:
    def __init__(self, engine: PaymentFeeEngine | None = None) -> None:
        self._engine = engine
        self._lock = asyncio.Lock()
        self._last_refresh_error: str | None = None

    def current(self) -> PaymentFeeEngine:
        if self._engine is None:
            raise ProviderDataUnavailable(
                "engine",
                "No payment fee engine is available yet.",
            )
        return self._engine

    @property
    def last_refresh_error(self) -> str | None:
        return self._last_refresh_error

    async def refresh(
        self,
        settings: Settings,
        *,
        raise_on_error: bool = False,
    ) -> list[ProviderInfo]:
        async with self._lock:
            try:
                new_engine = await asyncio.to_thread(
                    build_engine,
                    settings,
                    fail_on_error=True,
                )
                _run_smoke_quotes(new_engine)
            except Exception as exc:
                self._last_refresh_error = _error_message(exc)
                logger.warning("Refresh failed, retaining previous engine: %s", self._last_refresh_error)
                if raise_on_error:
                    raise
                if self._engine is None:
                    raise ProviderDataUnavailable(
                        "engine",
                        self._last_refresh_error,
                    ) from exc
                return self._engine.data_status()

            self._engine = new_engine
            self._last_refresh_error = None
            return self._engine.data_status()


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, PaymentFeeError):
        return exc.message
    return str(exc)


def _run_smoke_quotes(engine: PaymentFeeEngine) -> None:
    for provider_id in engine.providers():
        _smoke_quote(engine, provider_id)


def _smoke_quote(engine: PaymentFeeEngine, provider_id: str) -> None:
    markets = engine.markets(provider_id)
    if not markets:
        raise ProviderDataUnavailable(provider_id, "No markets available for smoke quote.")

    for market in markets:
        account_country = market.account_country
        try:
            cap = engine.capabilities(provider_id, account_country)
        except PaymentFeeError:
            continue

        product, variant = _first_calculable_product_variant(cap)
        if product is None:
            continue

        request: dict[str, Any] = _build_smoke_request(
            provider_id,
            account_country,
            product,
            variant,
            cap,
        )

        try:
            engine.quote(request)
            return
        except InsufficientTransactionContext:
            continue
        except QuoteNotAvailable:
            continue

    raise ProviderDataUnavailable(
        provider_id,
        "Smoke quote failed for all available markets.",
    )


def _first_calculable_product_variant(
    cap: CapabilityInfo,
) -> tuple[str | None, str | None]:
    for product, variants in cap.calculable_products.items():
        if variants:
            return product, variants[0]
        return product, None
    return None, None


def _build_smoke_request(
    provider_id: str,
    account_country: str,
    product: str,
    variant: str | None,
    cap: CapabilityInfo,
) -> dict[str, Any]:
    transaction: dict[str, Any] = {"product_id": product}
    if variant:
        transaction["variant_id"] = variant

    if provider_id == "paypal":
        transaction["transaction_region"] = "domestic"
        if "transaction.payment_method" in cap.required_context:
            transaction["payment_method"] = "paypal"
    elif provider_id == "stripe":
        if "transaction.payment_method" in cap.required_context or cap.payment_methods:
            transaction["payment_method"] = "card"
        if "transaction.channel" in cap.required_context:
            transaction["channel"] = "online"
        if "transaction.card.origin" in cap.required_context:
            transaction["card"] = {"origin": "domestic", "region": "domestic", "tier": "standard"}

    return {
        "provider": provider_id,
        "amount": {"value": "100", "currency": "USD"},
        "account_country": account_country,
        "customer_country": account_country,
        "settlement_currency": "USD",
        "transaction": transaction,
    }
