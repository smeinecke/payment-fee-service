from __future__ import annotations

from fastapi.testclient import TestClient
from payment_fee import PaymentFeeEngine
from payment_fee_service.app import create_app
from payment_fee_service.settings import Settings


def test_openapi_schema_is_canonical_v1(client: TestClient) -> None:
    response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Payment Fee Service"
    assert schema["info"]["version"] == "0.3.0"
    assert "/v1/quotes" in schema["paths"]
    assert "/v2/quotes" not in schema["paths"]
    assert all(path.startswith("/v1/") or path.startswith("/health/") for path in schema["paths"])


def test_health(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200


def test_providers(client: TestClient) -> None:
    response = client.get("/v1/providers")
    assert response.status_code == 200
    providers = {item["provider"]: item for item in response.json()}
    assert providers.keys() == {"paypal", "stripe"}
    assert all(item["ready"] for item in providers.values())


def test_markets_and_capabilities(client: TestClient) -> None:
    response = client.get("/v1/providers/stripe/markets")
    assert response.status_code == 200
    assert any(item["account_country"] == "DE" for item in response.json())

    response = client.get("/v1/providers/stripe/markets/DE/capabilities")
    assert response.status_code == 200
    assert "card" in response.json()["payment_methods"]


def test_quote_schema_endpoint(client: TestClient) -> None:
    response = client.get("/v1/providers/stripe/markets/DE/quote-schema")
    assert response.status_code == 200
    schema = response.json()
    assert schema["provider"] == "stripe"
    assert schema["account_country"] == "DE"
    assert "request_schema" in schema
    assert "response_schema" in schema


def test_data_status_endpoint(client: TestClient) -> None:
    response = client.get("/v1/data/status")
    assert response.status_code == 200
    statuses = {item["provider"]: item for item in response.json()}
    assert statuses.keys() == {"paypal", "stripe"}
    assert all(item["ready"] for item in statuses.values())


def _stripe_payload() -> dict:
    return {
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
    }


def _paypal_payload() -> dict:
    return {
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
    }


def test_stripe_quote(client: TestClient) -> None:
    response = client.post("/v1/quotes", json=_stripe_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "stripe"
    assert data["processing_fee"]["value"] == "1.75"
    assert data["net_amount"]["value"] == "98.25"


def test_paypal_quote(client: TestClient) -> None:
    response = client.post("/v1/quotes", json=_paypal_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "paypal"
    assert data["processing_fee"]["value"] == "3.38"
    assert data["net_amount"]["value"] == "96.62"


def test_http_matches_library_for_stripe(client: TestClient, engine: PaymentFeeEngine) -> None:
    payload = _stripe_payload()
    http = client.post("/v1/quotes", json=payload)
    assert http.status_code == 200
    direct = engine.quote(payload)
    assert http.json() == direct.model_dump(mode="json")


def test_http_matches_library_for_paypal(client: TestClient, engine: PaymentFeeEngine) -> None:
    payload = _paypal_payload()
    http = client.post("/v1/quotes", json=payload)
    assert http.status_code == 200
    direct = engine.quote(payload)
    assert http.json() == direct.model_dump(mode="json")


def test_v2_routes_return_404(client: TestClient) -> None:
    assert client.post("/v2/quotes", json=_stripe_payload()).status_code == 404
    assert client.get("/v2/providers").status_code == 404
    assert client.get("/v2/providers/stripe/markets").status_code == 404
    assert client.get("/v2/providers/stripe/markets/DE/capabilities").status_code == 404
    assert client.get("/v2/providers/stripe/markets/DE/quote-schema").status_code == 404
    assert client.get("/v2/data/status").status_code == 404
    assert client.post("/v2/data/refresh").status_code == 404


def test_legacy_payment_object_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "stripe",
            "amount": {"value": "100.00", "currency": "EUR"},
            "account_country": "DE",
            "payment": {
                "method": "card",
                "channel": "online",
                "card": {"origin": "domestic", "region": "domestic", "tier": "standard"},
            },
        },
    )
    assert response.status_code == 422


def test_legacy_paypal_transaction_type_is_rejected(client: TestClient) -> None:
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


def test_structured_error(client: TestClient) -> None:
    response = client.post(
        "/v1/quotes",
        json={
            "provider": "paypal",
            "amount": {"value": "100.00", "currency": "XXX"},
            "account_country": "DE",
            "transaction": {"product_id": "other_commercial", "transaction_region": "domestic"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUOTE_NOT_AVAILABLE"


def test_refresh_endpoint_disabled_without_admin_token(engine: PaymentFeeEngine) -> None:
    settings = Settings(
        refresh_interval_seconds=0,
        admin_token=None,
        providers={},
    )
    with TestClient(create_app(settings=settings, engine=engine)) as client:
        response = client.post("/v1/data/refresh")
        assert response.status_code == 404


def test_refresh_endpoint_rejects_invalid_token(client: TestClient) -> None:
    response = client.post(
        "/v1/data/refresh",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_refresh_endpoint_with_valid_token(client: TestClient) -> None:
    response = client.post(
        "/v1/data/refresh",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert {item["provider"] for item in response.json()} == {"paypal", "stripe"}
