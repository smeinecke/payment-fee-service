from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from payment_fee import PaymentFeeEngine
from payment_fee.errors import DatasetValidationError
from payment_fee.providers.paypal import PayPalProvider
from payment_fee.providers.stripe import StripeProvider

PAYPAL_DATA = Path(__file__).parent.parent.parent / "paypal-fee-data"
STRIPE_DATA = Path(__file__).parent.parent.parent / "stripe-fee-data"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _paypal_schemas() -> dict[str, Any]:
    return {
        "core": _load_json(PAYPAL_DATA / "schemas/core-fees-v1.schema.json"),
        "index": _load_json(PAYPAL_DATA / "schemas/index-v1.schema.json"),
    }


def _stripe_schemas() -> dict[str, Any]:
    return {
        "core": _load_json(STRIPE_DATA / "schemas/core-fees-v1.schema.json"),
        "index": _load_json(STRIPE_DATA / "schemas/index-v1.schema.json"),
    }


@pytest.mark.skipif(not (PAYPAL_DATA / "json/core-fees.json").exists(), reason="paypal-fee-data not available")
def test_paypal_provider_from_documents_validate_true_requires_schemas() -> None:
    core = _load_json(PAYPAL_DATA / "json/core-fees.json")
    index = _load_json(PAYPAL_DATA / "json/index.json")
    with pytest.raises(DatasetValidationError) as exc:
        PayPalProvider.from_documents(core=core, index=index, validate_schema=True)
    assert "core schema is required" in str(exc.value).lower()


@pytest.mark.skipif(not (PAYPAL_DATA / "json/core-fees.json").exists(), reason="paypal-fee-data not available")
def test_paypal_provider_from_documents_validate_true_with_schemas() -> None:
    core = _load_json(PAYPAL_DATA / "json/core-fees.json")
    index = _load_json(PAYPAL_DATA / "json/index.json")
    provider = PayPalProvider.from_documents(
        core=core,
        index=index,
        schemas=_paypal_schemas(),
        validate_schema=True,
    )
    assert provider.provider_id == "paypal"


@pytest.mark.skipif(not (STRIPE_DATA / "json/core-fees.json").exists(), reason="stripe-fee-data not available")
def test_stripe_provider_from_documents_validate_true_requires_schemas() -> None:
    core = _load_json(STRIPE_DATA / "json/core-fees.json")
    index = _load_json(STRIPE_DATA / "json/index.json")
    with pytest.raises(DatasetValidationError) as exc:
        StripeProvider.from_documents(
            core=core,
            index=index,
            validate_schema=True,
        )
    assert "core schema is required" in str(exc.value).lower()


@pytest.mark.skipif(not (STRIPE_DATA / "json/core-fees.json").exists(), reason="stripe-fee-data not available")
def test_stripe_provider_from_documents_validate_true_with_schemas() -> None:
    core = _load_json(STRIPE_DATA / "json/core-fees.json")
    index = _load_json(STRIPE_DATA / "json/index.json")
    provider = StripeProvider.from_documents(
        core=core,
        index=index,
        schemas=_stripe_schemas(),
        validate_schema=True,
    )
    assert provider.provider_id == "stripe"


@pytest.mark.skipif(
    not (PAYPAL_DATA / "json/core-fees.json").exists() or not (STRIPE_DATA / "json/core-fees.json").exists(),
    reason="provider fee data not available",
)
def test_engine_from_documents_validate_true() -> None:
    paypal_core = _load_json(PAYPAL_DATA / "json/core-fees.json")
    paypal_index = _load_json(PAYPAL_DATA / "json/index.json")
    stripe_core = _load_json(STRIPE_DATA / "json/core-fees.json")
    stripe_index = _load_json(STRIPE_DATA / "json/index.json")
    engine = PaymentFeeEngine.from_documents(
        paypal={
            "core": paypal_core,
            "index": paypal_index,
            "schemas": _paypal_schemas(),
        },
        stripe={
            "core": stripe_core,
            "index": stripe_index,
            "schemas": _stripe_schemas(),
        },
        validate=True,
    )
    assert sorted(engine.providers()) == ["paypal", "stripe"]
