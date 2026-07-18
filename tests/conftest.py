from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from payment_fee import PaymentFeeEngine
from payment_fee_service.app import create_app
from payment_fee_service.settings import ProviderSettings, Settings

REPO_ROOT = Path(__file__).parent.parent.resolve()
PAYPAL_DATA = Path(os.environ.get("PAYPAL_FEE_DATA", REPO_ROOT.parent / "paypal-fee-data")).resolve()
STRIPE_DATA = Path(os.environ.get("STRIPE_FEE_DATA", REPO_ROOT.parent / "stripe-fee-data")).resolve()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        refresh_interval_seconds=0,
        admin_token="test-token",
        validate_json_schema=False,
        providers={
            "paypal": ProviderSettings(
                data_path=PAYPAL_DATA,
                data_ref="test",
            ),
            "stripe": ProviderSettings(
                data_path=STRIPE_DATA,
                data_ref="test",
            ),
        },
    )


@pytest.fixture
def engine(settings: Settings) -> PaymentFeeEngine:
    from payment_fee_service.bootstrap import build_engine

    return build_engine(settings)


@pytest.fixture
def client(settings: Settings, engine: PaymentFeeEngine):
    with TestClient(create_app(settings=settings, engine=engine)) as test_client:
        yield test_client
