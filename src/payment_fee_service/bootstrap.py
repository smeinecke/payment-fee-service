from __future__ import annotations

import asyncio

from fastapi import FastAPI

from payment_fee_service.data.source import DataLocation, JsonDataSource
from payment_fee_service.providers import load_provider_module
from payment_fee_service.providers.registry import ProviderRegistry
from payment_fee_service.service import QuoteService
from payment_fee_service.settings import Settings


def build_registry(settings: Settings, fail_on_error: bool | None = None) -> ProviderRegistry:
    if fail_on_error is None:
        fail_on_error = settings.fail_startup_on_data_error

    registry = ProviderRegistry()
    for provider_id, provider_settings in settings.providers.items():
        if not provider_settings.enabled:
            continue
        try:
            module = load_provider_module(provider_id)
            provider_class = module.Provider
            repository_class = module.Repository
            source = JsonDataSource(
                DataLocation(
                    provider=provider_id,
                    local_path=provider_settings.data_path,
                    base_url=provider_settings.data_url,
                    data_ref=provider_settings.data_ref,
                ),
                settings.http_timeout_seconds,
            )
            repository = repository_class(source, validate_schema=settings.validate_json_schema)
            registry.register(provider_class(repository.load()))
        except Exception as exc:  # Startup must expose provider-specific readiness failures.
            if fail_on_error:
                raise
            registry.register_error(provider_id, exc)
    return registry


async def refresh_registry(app: FastAPI, settings: Settings) -> ProviderRegistry:
    new_registry = await asyncio.to_thread(build_registry, settings, False)
    app.state.registry = new_registry
    app.state.quote_service = QuoteService(new_registry)
    return new_registry
