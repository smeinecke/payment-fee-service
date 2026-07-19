from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from click.testing import CliRunner
from paypal_sandbox_validation.cli import cli
from paypal_sandbox_validation.error_classification import classify_paypal_api_error
from paypal_sandbox_validation.models import Case, CaseStatus, ReconciliationStatus
from paypal_sandbox_validation.paypal_api import PayPalAPIError
from paypal_sandbox_validation.persistence import save_plan, save_results
from paypal_sandbox_validation.planner import ensure_surcharge_case
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.reporting import save_junit
from paypal_sandbox_validation.url_validation import (
    URLValidationError,
    validate_api_url,
    validate_approval_url,
    validate_callback_url,
)


def _us_only_csv(tmp_path: Path, client_id: str = "A" * 80, secret: str = "B" * 80) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret\n"
        f"US;BUSINESS;merchant-us@business.example.com;secret1;Test;MerchantUS;99999;Y;VISA;4111111111111111;{client_id};{secret}\n"
        "US;PERSONAL;buyer-us@personal.example.com;secret2;Test;BuyerUS;99999;Y;VISA;4111111111111111;;;\n",
        encoding="utf-8",
    )
    return path


def test_validate_api_url_accepts_sandbox_api() -> None:
    validate_api_url("https://api-m.sandbox.paypal.com/v1/oauth2/token")


def test_validate_api_url_rejects_live_host() -> None:
    with pytest.raises(URLValidationError):
        validate_api_url("https://api-m.paypal.com/v1/oauth2/token")


def test_validate_api_url_rejects_wrong_port() -> None:
    with pytest.raises(URLValidationError):
        validate_api_url("https://api-m.sandbox.paypal.com:8080/v1/oauth2/token")


def test_validate_approval_url_accepts_sandbox() -> None:
    validate_approval_url("https://www.sandbox.paypal.com/checkoutnow?token=ABC")


def test_validate_approval_url_rejects_live() -> None:
    with pytest.raises(URLValidationError):
        validate_approval_url("https://www.paypal.com/checkoutnow?token=ABC")


def test_validate_callback_url_accepts_loopback() -> None:
    validate_callback_url("http://127.0.0.1:8123/paypal/return")


def test_validate_callback_url_rejects_non_loopback() -> None:
    with pytest.raises(URLValidationError):
        validate_callback_url("http://192.168.1.1:8123/paypal/return")


def test_validate_config_fails_on_invalid_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path, client_id="same", secret="same")
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-config", "--accounts-csv", str(path)])
    assert result.exit_code == 1
    assert "merchants_valid = 0" in result.output


def test_probe_fails_on_invalid_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path, client_id="same", secret="same")
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    runner = CliRunner()
    result = runner.invoke(cli, ["probe", "--accounts-csv", str(path)])
    assert result.exit_code == 1
    assert "Account configuration is invalid" in result.output


def test_reconcile_buyer_country_mismatch() -> None:
    quote = {
        "gross_amount": {"value": "10.00", "currency": "USD"},
        "processing_fee": {"value": "0.69", "currency": "USD"},
        "net_amount": {"value": "9.31", "currency": "USD"},
    }
    evidence = {
        "gross_amount": {"currency_code": "USD", "value": "10.00"},
        "paypal_fee": {"currency_code": "USD", "value": "0.69"},
        "net_amount": {"currency_code": "USD", "value": "9.31"},
        "payer_country": "CA",
    }
    result = reconcile(evidence, quote, "US", "US", "CA")
    assert result.status == ReconciliationStatus.BUYER_COUNTRY_MISMATCH


def test_classify_paypal_api_error_compliance() -> None:
    exc = PayPalAPIError(
        "compliance",
        status_code=422,
        body={"name": "COMPLIANCE_VIOLATION", "debug_id": "DBG123"},
        operation="create order",
    )
    status, safe, detail = classify_paypal_api_error(exc)
    assert status == ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE
    assert safe["name"] == "COMPLIANCE_VIOLATION"
    assert safe["debug_id"] == "DBG123"
    assert "compliance" in detail.lower()


def test_smoke_plan_has_surcharge_bearing_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os.environ.setdefault(
        "PAYPAL_FEE_DATA_PATH",
        str(Path(__file__).resolve().parents[1] / "paypal-fee-data"),
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.configuration import load_scenarios
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products

    scenarios = load_scenarios()
    run_id = "test-run-1234"
    plan = build_plan(
        run_id=run_id,
        profile_name="smoke",
        scenarios=scenarios,
    )
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    plan = ensure_surcharge_case(plan, adapter)

    domestic = [c for c in plan if c.merchant_country == c.buyer_country]
    cross_border = [c for c in plan if c.merchant_country != c.buyer_country]
    assert all(c.expected_surcharge_components == 0 for c in domestic)
    assert any(c.expected_surcharge_components > 0 for c in cross_border)


def test_resume_reconciled_case_does_not_call_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os.environ.setdefault(
        "PAYPAL_FEE_DATA_PATH",
        str(Path(__file__).resolve().parents[1] / "paypal-fee-data"),
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products

    path = _us_only_csv(tmp_path)
    scenarios = {
        "standard_wallet_checkout": {
            "provider": "paypal",
            "channel": "online",
            "payment_method": "paypal_wallet",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "intent": "CAPTURE",
            "default": {"product_id": "other_commercial", "variant_id": "standard"},
            "per_country": {"US": {"product_id": "paypal_checkout", "variant_id": "standard"}},
        },
        "profiles": {
            "smoke": {
                "merchants": ["US"],
                "buyers_per_merchant": ["US"],
                "amount": "10.00",
                "currencies": {"US": "USD"},
            }
        },
    }
    run_id = "resume-test-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    case = plan[0]
    case.quote = case.quote or adapter.build_quote("US", "US", "10.00", "USD")
    case.order_id = "DUMMYORDERID"
    case.capture_id = "DUMMYCAPTUREID"
    case.status = CaseStatus.RECONCILED
    case.reconciliation = {
        "status": "match",
        "delta_minor_units": 0,
        "paypal_fee_value": "0.84",
        "library_fee_value": "0.84",
    }
    save_results(run_id, {"run_id": run_id, "cases": [case.model_dump()]})

    create_called: list[Case] = []
    original_create = "paypal_sandbox_validation.cli._create_order"

    def fake_create(*args: object, **kwargs: object) -> None:
        create_called.append(args[0])  # type: ignore[index]
        return None

    monkeypatch.setattr(original_create, fake_create)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--accounts-csv",
            str(path),
            "--resume",
            run_id,
            "--merchant",
            "US",
            "--buyer",
            "US",
        ],
    )
    assert result.exit_code == 0, result.output
    assert create_called == []


def test_junit_counts_match_results(tmp_path: Path) -> None:
    run_id = "junit-test-001"
    artifact_dir = tmp_path / "artifacts" / "paypal-sandbox" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / "plan.json"
    plan_path.write_text("[]")
    summary = {
        "run_id": run_id,
        "planned": 4,
        "cases": [
            {"case_id": "c1", "status": "reconciled", "reconciliation_status": "match"},
            {"case_id": "c2", "status": "reconciled", "reconciliation_status": "fee_mismatch"},
            {"case_id": "c3", "status": "failed", "reconciliation_status": "paypal_api_failure"},
            {"case_id": "c4", "status": "skipped", "reconciliation_status": None},
        ],
    }
    junit_path = save_junit(run_id, summary)
    root = ET.fromstring(junit_path.read_bytes())
    assert root.get("tests") == "4"
    assert root.get("failures") == "1"
    assert root.get("errors") == "1"
    assert root.get("skipped") == "1"
    skipped = sum(1 for child in root.iter("testcase") if child.find("skipped") is not None)
    assert skipped == 1
