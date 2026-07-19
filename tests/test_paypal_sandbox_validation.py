from __future__ import annotations

import os
from pathlib import Path

import pytest
from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.configuration import load_scenarios
from paypal_sandbox_validation.models import Account, AccountType, ReconciliationStatus
from paypal_sandbox_validation.oauth import OAuthCache
from paypal_sandbox_validation.persistence import load_plan, save_plan
from paypal_sandbox_validation.planner import build_plan, generate_request_id, generate_run_id
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.redaction import (
    mask_email,
    mask_value,
    redact_path,
    sanitize_paypal_capture,
    sanitize_paypal_order,
)

SAMPLE_CSV = """country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret
DE;BUSINESS;merchant-de@business.example.com;secret1;Test;MerchantDE;99999;Y;VISA;4111111111111111;{cid};{sec}
DE;PERSONAL;buyer-de@personal.example.com;secret2;Test;BuyerDE;99999;Y;VISA;4111111111111111;;
US;BUSINESS;pp-merchant-us@business.example.com;secret3;Test;MerchantUS;99999;Y;VISA;4111111111111111;{cid2};{sec2}
US;PERSONAL;pp-buyer-us@personal.example.com;secret4;Test;BuyerUS;99999;Y;VISA;4111111111111111;;
"""  # noqa: E501


def _sample_csv(tmp_path: Path, cid: str = "A" * 80, sec: str = "B" * 80) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(
        SAMPLE_CSV.format(cid=cid, sec=sec, cid2=cid + "2", sec2=sec + "2"),
        encoding="utf-8",
    )
    return path


def test_parse_accounts_csv(tmp_path: Path) -> None:
    path = _sample_csv(tmp_path)
    accounts = parse_accounts_csv(path)
    assert len(accounts) == 4
    assert all(isinstance(a, Account) for a in accounts)
    merchants = [a for a in accounts if a.is_business()]
    buyers = [a for a in accounts if a.is_personal()]
    assert len(merchants) == 2
    assert len(buyers) == 2


def test_validate_accounts_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _sample_csv(tmp_path)
    accounts = parse_accounts_csv(path)
    # Restrict expected countries to the ones present in the sample so the
    # validation does not fail because other countries are missing.
    monkeypatch.setattr(
        "paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES",
        {"DE", "US"},
    )
    result = validate_accounts(accounts)
    assert result["valid"] is True
    assert result["duplicate_accounts"] == []
    assert result["duplicate_client_ids"] == []


def test_validate_accounts_detects_duplicate_buyer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _sample_csv(tmp_path)
    accounts = parse_accounts_csv(path)
    accounts.append(
        Account(
            country_code="US",
            account_type=AccountType.PERSONAL,
            primary_email_alias="pp-buyer-us@personal.example.com",
            password="x",
            first_name="Test",
            last_name="BuyerUS2",
        )
    )
    monkeypatch.setattr(
        "paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES",
        {"DE", "US"},
    )
    result = validate_accounts(accounts)
    assert result["valid"] is False
    assert "US" in result["duplicate_accounts"]


def test_load_scenarios() -> None:
    scenarios = load_scenarios()
    assert "standard_wallet_checkout" in scenarios


def test_quote_adapter_domestic_de() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", str(Path(__file__).resolve().parents[1] / "paypal-fee-data"))
    adapter = QuoteAdapter()
    quote = adapter.build_quote("DE", "DE", "10.00", "EUR")
    assert quote["amount"]["currency"] == "EUR"
    assert quote["amount"]["value"] == "10.00"
    assert quote["processing_fee"]["currency"] == "EUR"
    assert float(quote["processing_fee"]["value"]) > 0
    assert quote["net_amount"]["currency"] == "EUR"


def test_quote_adapter_cross_border_de_to_us() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", str(Path(__file__).resolve().parents[1] / "paypal-fee-data"))
    adapter = QuoteAdapter()
    quote = adapter.build_quote("DE", "US", "10.00", "EUR")
    assert quote["processing_fee"]["currency"] == "EUR"
    assert float(quote["processing_fee"]["value"]) > float(
        QuoteAdapter().build_quote("DE", "DE", "10.00", "EUR")["processing_fee"]["value"]
    )


def test_quote_adapter_zero_decimal_jpy() -> None:
    os.environ.setdefault("PAYPAL_FEE_DATA_PATH", str(Path(__file__).resolve().parents[1] / "paypal-fee-data"))
    # Japan has zero-decimal JPY but the public PayPal fee dataset does not
    # expose a paypal_checkout schedule for it.  Verify the helper that
    # converts zero-decimal amounts to minor units.
    from paypal_sandbox_validation.quote_adapter import minor_units

    assert minor_units("1000", "JPY") == 1000
    assert minor_units("10.00", "EUR") == 1000
    assert minor_units("10.000", "BHD") == 10000


def test_reconcile_match() -> None:
    quote = {
        "gross_amount": {"value": "10.00", "currency": "EUR"},
        "processing_fee": {"value": "0.69", "currency": "EUR"},
        "net_amount": {"value": "9.31", "currency": "EUR"},
    }
    evidence = {
        "gross_amount": {"currency_code": "EUR", "value": "10.00"},
        "paypal_fee": {"currency_code": "EUR", "value": "0.69"},
        "net_amount": {"currency_code": "EUR", "value": "9.31"},
        "payer_country": "DE",
    }
    result = reconcile(evidence, quote, "DE", "DE", "DE")
    assert result.status == ReconciliationStatus.MATCH
    assert result.delta_minor_units == 0


def test_reconcile_fee_mismatch() -> None:
    quote = {
        "gross_amount": {"value": "10.00", "currency": "EUR"},
        "processing_fee": {"value": "0.69", "currency": "EUR"},
        "net_amount": {"value": "9.31", "currency": "EUR"},
    }
    evidence = {
        "gross_amount": {"currency_code": "EUR", "value": "10.00"},
        "paypal_fee": {"currency_code": "EUR", "value": "0.89"},
        "net_amount": {"currency_code": "EUR", "value": "9.11"},
        "payer_country": "DE",
    }
    result = reconcile(evidence, quote, "DE", "DE", "DE")
    assert result.status == ReconciliationStatus.FEE_MISMATCH


def test_reconcile_currency_mismatch() -> None:
    quote = {
        "gross_amount": {"value": "10.00", "currency": "EUR"},
        "processing_fee": {"value": "0.69", "currency": "EUR"},
        "net_amount": {"value": "9.31", "currency": "EUR"},
    }
    evidence = {
        "gross_amount": {"currency_code": "USD", "value": "10.00"},
        "paypal_fee": {"currency_code": "USD", "value": "0.69"},
        "net_amount": {"currency_code": "USD", "value": "9.31"},
        "payer_country": "DE",
    }
    result = reconcile(evidence, quote, "DE", "DE", "DE")
    assert result.status == ReconciliationStatus.CURRENCY_MISMATCH


def test_redaction() -> None:
    assert mask_email("test@example.com") == "t***@example.com"
    assert mask_value("client_id", "Abc1234567890XYZ") == "Abc1...0XYZ"
    assert redact_path("/home/user/paypal-sandbox-accounts.csv") == "paypal-sandbox-accounts.csv"


def test_sanitize_paypal_order() -> None:
    order = {
        "id": "ORDER1234567890",
        "links": [
            {"href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORDER1234567890", "rel": "self"},
            {"href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1234567890", "rel": "approve"},
        ],
        "purchase_units": [
            {
                "amount": {"currency_code": "EUR", "value": "10.00"},
                "payee": {"email_address": "merchant@example.com", "merchant_id": "ABC"},
            }
        ],
    }
    sanitized = sanitize_paypal_order(order)
    assert sanitized["id"].startswith("ORDE")
    assert "%2A%2A%2A" in sanitized["links"][1]["href"]
    assert "***" in sanitized["purchase_units"][0]["payee"]["email_address"]


def test_sanitize_paypal_capture() -> None:
    capture = {
        "id": "CAP123",
        "payer": {"email_address": "buyer@example.com", "payer_id": "PAYER"},
        "purchase_units": [
            {
                "payments": {
                    "captures": [
                        {
                            "id": "CAP123",
                            "amount": {"currency_code": "EUR", "value": "10.00"},
                            "seller_receivable_breakdown": {
                                "paypal_fee": {"currency_code": "EUR", "value": "0.69"},
                                "net_amount": {"currency_code": "EUR", "value": "9.31"},
                            },
                        }
                    ]
                }
            }
        ],
    }
    sanitized = sanitize_paypal_capture(capture)
    assert "***" in sanitized["payer"]["email_address"]
    assert sanitized["purchase_units"][0]["payments"]["captures"][0]["id"].startswith("CAP")


def test_callback_server() -> None:
    import httpx

    server = CallbackServer(expected_token="TOKEN123")
    server.start()
    try:
        response = httpx.get(server.return_url, params={"token": "TOKEN123"}, timeout=5)
        assert response.status_code == 200
        assert server.wait_for_state(timeout=5.0) == "approved"
    finally:
        server.stop()


def test_build_plan_smoke() -> None:
    scenarios = load_scenarios()
    run_id = generate_run_id()
    plan = build_plan(
        run_id=run_id,
        profile_name="smoke",
        scenarios=scenarios,
    )
    assert len(plan) >= 1
    assert plan[0].run_id == run_id


def test_plan_save_and_load(tmp_path: Path) -> None:
    scenarios = load_scenarios()
    run_id = generate_run_id()
    plan = build_plan(
        run_id=run_id,
        profile_name="smoke",
        scenarios=scenarios,
    )
    save_plan(run_id, plan)
    loaded = load_plan(run_id)
    assert len(loaded) == len(plan)
    assert loaded[0].case_id == plan[0].case_id


def test_oauth_cache() -> None:
    cache = OAuthCache()
    cache.set("key", "token")
    assert cache.get("key") == "token"
    assert cache.get("missing") is None


def test_generate_request_id_format() -> None:
    rid = generate_request_id("run", "case", "create", 0)
    assert rid.startswith("run-case-create-")


@pytest.mark.live
@pytest.mark.e2e
class TestPayPalSandboxSmoke:
    """Live smoke tests; skipped unless --live and --e2e are passed."""

    def test_smoke_placeholder(self) -> None:
        pytest.skip("Live PayPal Sandbox smoke tests require real credentials and a browser.")
