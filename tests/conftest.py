from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from payment_fee_service.app import create_app
from payment_fee_service.data.source import DataLocation, JsonDataSource
from payment_fee_service.providers import load_provider_module
from payment_fee_service.providers.registry import ProviderRegistry
from payment_fee_service.settings import ProviderSettings, Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        refresh_interval_seconds=0,
        admin_token="test-token",
        providers={
            "paypal": ProviderSettings(
                data_path=FIXTURES / "paypal",
                data_ref="fixture-paypal",
            ),
            "stripe": ProviderSettings(
                data_path=FIXTURES / "stripe",
                data_ref="fixture-stripe",
            ),
        },
    )


@pytest.fixture
def registry(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider_id, provider_settings in settings.providers.items():
        module = load_provider_module(provider_id)
        source = JsonDataSource(
            DataLocation(
                provider=provider_id,
                local_path=provider_settings.data_path,
                base_url=None,
                data_ref=provider_settings.data_ref,
            ),
            settings.http_timeout_seconds,
        )
        repository = module.Repository(source, validate_schema=False)
        registry.register(module.Provider(repository.load()))
    return registry


@pytest.fixture
def client(settings: Settings, registry: ProviderRegistry):
    with TestClient(create_app(settings=settings, registry=registry)) as test_client:
        yield test_client
