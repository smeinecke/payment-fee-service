from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.e2e


def test_health(client: httpx.Client) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_providers(client: httpx.Client) -> None:
    response = client.get("/v1/providers")
    assert response.status_code == 200
    providers = {item["provider"]: item for item in response.json()}
    assert providers.keys() == {"paypal", "stripe"}
    assert all(item["ready"] for item in providers.values())


def test_markets_and_capabilities(client: httpx.Client) -> None:
    response = client.get("/v1/providers/stripe/markets")
    assert response.status_code == 200
    assert any(item["account_country"] == "DE" for item in response.json())

    response = client.get("/v1/providers/stripe/markets/DE/capabilities")
    assert response.status_code == 200
    assert "card" in response.json()["payment_methods"]


def test_paypal_quote(client: httpx.Client) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "transaction": {
                "product_id": "other_commercial",
                "variant_id": "standard",
                "transaction_region": "domestic",
            },
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "paypal"
    assert data["processing_fee"]["value"] == "3.38"
    assert data["net_amount"]["value"] == "96.62"


def test_stripe_quote(client: httpx.Client) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "transaction": {
                "product_id": "payments",
                "variant_id": "online_domestic_cards",
                "payment_method": "card",
                "channel": "online",
                "pricing_tier": "standard",
                "card": {
                    "origin": "domestic",
                    "region": "domestic",
                    "tier": "standard",
                },
            },
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "stripe"
    assert data["processing_fee"]["value"] == "1.75"
    assert data["net_amount"]["value"] == "98.25"


def test_v2_routes_are_absent(client: httpx.Client) -> None:
    assert client.post("/v2/quotes", json={"provider": "stripe"}).status_code == 404
    assert client.get("/v2/providers").status_code == 404


def test_legacy_payment_request_is_rejected(client: httpx.Client) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "payment": {"transaction_type": "standard_commercial"},
        },
    )
    assert response.status_code == 422
