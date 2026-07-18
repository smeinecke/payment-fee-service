from fastapi.testclient import TestClient
from payment_fee_service.app import create_app
from payment_fee_service.settings import Settings


def test_openapi_schema_is_served_dynamically(client) -> None:
    response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Payment Fee Service"
    assert "/v1/quotes" in schema["paths"]


def test_health(client) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200


def test_provider_and_market_discovery(client) -> None:
    providers = client.get("/v1/providers")
    assert providers.status_code == 200
    assert {item["provider"] for item in providers.json()} == {"paypal", "stripe"}

    markets = client.get("/v1/providers/stripe/markets")
    assert markets.status_code == 200
    assert any(item["account_country"] == "DE" for item in markets.json())

    capabilities = client.get("/v1/providers/stripe/markets/DE/capabilities")
    assert capabilities.status_code == 200
    assert "card" in capabilities.json()["payment_methods"]


def test_quote_endpoint(client) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "payment": {"transaction_type": "standard_commercial"},
        },
    )
    assert response.status_code == 200
    assert response.json()["processing_fee"]["value"] == "3.38"


def test_structured_error(client) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "XXX"},
            "account_country": "DE",
            "payment": {"transaction_type": "standard_commercial"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUOTE_NOT_AVAILABLE"


def test_refresh_endpoint_disabled_without_admin_token(engine) -> None:
    settings = Settings(
        refresh_interval_seconds=0,
        admin_token=None,
        providers={},
    )
    with TestClient(create_app(settings=settings, engine=engine)) as client:
        response = client.post("/v1/data/refresh")
        assert response.status_code == 404


def test_refresh_endpoint_rejects_invalid_token(client) -> None:
    response = client.post(
        "/v1/data/refresh",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_refresh_endpoint_with_valid_token(client) -> None:
    response = client.post(
        "/v1/data/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert {item["provider"] for item in response.json()} == {"paypal", "stripe"}
