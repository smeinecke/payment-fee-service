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


def test_v2_quote_matches_v1_for_paypal(client) -> None:
    v1 = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "payment": {"transaction_type": "standard_commercial"},
        },
    )
    assert v1.status_code == 200
    v2 = client.post(
        "/v2/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "transaction": {"product_id": "other_commercial", "transaction_region": "domestic"},
        },
    )
    assert v2.status_code == 200
    assert v1.json()["processing_fee"] == v2.json()["processing_fee"]
    assert v1.json()["net_amount"] == v2.json()["net_amount"]


def test_v2_quote_matches_v1_for_stripe(client) -> None:
    v1 = client.post(
        "/v1/quotes",
        json={
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "customer_country": "DE",
            "settlement_currency": "EUR",
            "payment": {
                "method": "card",
                "channel": "online",
                "card": {"origin": "domestic", "region": "eea", "tier": "standard"},
            },
        },
    )
    assert v1.status_code == 200
    v2 = client.post(
        "/v2/quotes",
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
                "card": {"origin": "domestic", "region": "domestic", "tier": "standard"},
            },
        },
    )
    assert v2.status_code == 200
    assert v1.json()["processing_fee"] == v2.json()["processing_fee"]
    assert v1.json()["net_amount"] == v2.json()["net_amount"]


def test_quote_schema_endpoint(client) -> None:
    response = client.get("/v2/providers/stripe/markets/DE/quote-schema")
    assert response.status_code == 200
    schema = response.json()
    assert schema["provider"] == "stripe"
    assert schema["account_country"] == "DE"
    assert "request_schema" in schema
    assert "response_schema" in schema


def test_data_status_endpoint(client) -> None:
    response = client.get("/v1/data/status")
    assert response.status_code == 200
    statuses = {item["provider"]: item for item in response.json()}
    assert statuses.keys() == {"paypal", "stripe"}
    assert all(item["ready"] for item in statuses.values())


def test_structured_error_v2(client) -> None:
    response = client.post(
        "/v2/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "XXX"},
            "account_country": "DE",
            "transaction": {"product_id": "other_commercial", "transaction_region": "domestic"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUOTE_NOT_AVAILABLE"
