from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from paypal_sandbox_validation.cli import cli
from paypal_sandbox_validation.error_classification import classify_paypal_api_error
from paypal_sandbox_validation.models import Account, Case, CaseStatus, ReconciliationStatus
from paypal_sandbox_validation.paypal_api import PayPalAPIError
from paypal_sandbox_validation.persistence import save_plan, save_results
from paypal_sandbox_validation.planner import ensure_surcharge_case
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.reporting import build_summary, save_junit
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


def _two_country_csv(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret\n"
        "US;BUSINESS;merchant-us@business.example.com;secret1;Test;MerchantUS;99999;Y;VISA;4111111111111111;USCLIENTID;USSECRET\n"
        "CA;BUSINESS;merchant-ca@business.example.com;secret2;Test;MerchantCA;99999;Y;VISA;4111111111111111;CACLIENTID;CASECRET\n"
        "US;PERSONAL;buyer-us@personal.example.com;secret3;Test;BuyerUS;99999;Y;VISA;4111111111111111;;;\n"
        "CA;PERSONAL;buyer-ca@personal.example.com;secret4;Test;BuyerCA;99999;Y;VISA;4111111111111111;;;\n",
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


def test_malicious_sandbox_looking_hosts() -> None:
    with pytest.raises(URLValidationError):
        validate_api_url("https://api-m.sandbox.paypal.com.attacker.example.com/v1")
    with pytest.raises(URLValidationError):
        validate_api_url("https://evil-sandbox.example.com/path")
    with pytest.raises(URLValidationError):
        validate_api_url("http://api-m.sandbox.paypal.com/v1")
    with pytest.raises(URLValidationError):
        validate_api_url("https://user:pass@api-m.sandbox.paypal.com/v1")
    with pytest.raises(URLValidationError):
        validate_approval_url("https://www.sandbox.paypal.com.attacker.example/checkoutnow")
    with pytest.raises(URLValidationError):
        validate_approval_url("https://sandbox.paypal.example/checkoutnow")


def test_complete_probe_requires_all_selected_merchants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "accounts.csv"
    path.write_text(
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret\n"
        "US;BUSINESS;merchant-us@business.example.com;secret1;Test;MerchantUS;99999;Y;VISA;4111111111111111;AAAA;BBBB\n"
        "CA;BUSINESS;merchant-ca@business.example.com;secret2;Test;MerchantCA;99999;Y;VISA;4111111111111111;CCCC;DDDD\n"
        "US;PERSONAL;buyer-us@personal.example.com;secret3;Test;BuyerUS;99999;Y;VISA;4111111111111111;;;\n"
        "CA;PERSONAL;buyer-ca@personal.example.com;secret4;Test;BuyerCA;99999;Y;VISA;4111111111111111;;;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US", "CA"})

    calls: list[str] = []

    def fake_probe(client_id: str, secret: str, country: str) -> object:
        calls.append(country)
        from paypal_sandbox_validation.models import OAuthProbeResult, OAuthProbeStatus

        return OAuthProbeResult(country=country, status=OAuthProbeStatus.SUCCESS)

    monkeypatch.setattr("paypal_sandbox_validation.cli.probe_credentials", fake_probe)
    runner = CliRunner()
    result = runner.invoke(cli, ["probe", "--accounts-csv", str(path)])
    assert result.exit_code == 0, result.output
    assert "merchants_present = 2" in result.output
    assert "merchants_probed = 2" in result.output
    assert "oauth_successful = 2" in result.output
    assert sorted(calls) == ["CA", "US"]

    # Now make one merchant fail.
    calls.clear()
    fail_count = {"CA": True}

    def fake_probe_fail(client_id: str, secret: str, country: str) -> object:
        calls.append(country)
        from paypal_sandbox_validation.models import OAuthProbeResult, OAuthProbeStatus

        if fail_count.get(country):
            return OAuthProbeResult(
                country=country, status=OAuthProbeStatus.INVALID_CLIENT, classification="invalid_client"
            )
        return OAuthProbeResult(country=country, status=OAuthProbeStatus.SUCCESS)

    monkeypatch.setattr("paypal_sandbox_validation.cli.probe_credentials", fake_probe_fail)
    result = runner.invoke(cli, ["probe", "--accounts-csv", str(path)])
    assert result.exit_code == 1, result.output
    assert "merchants_present = 2" in result.output
    assert "oauth_successful = 1" in result.output
    assert "oauth_failed = 1" in result.output


def test_resume_after_capture_runs_reconcile_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path)
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products
    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

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
    run_id = "resume-capture-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    case = plan[0]
    case.quote = adapter.build_quote("US", "US", "10.00", "USD")
    case.status = CaseStatus.CAPTURED
    case.order_id = "DUMMYORDER"
    case.capture_id = "DUMMYCAPTURE"
    case.request_id_create = "req-create"
    case.request_id_capture = "req-capture"
    case.paypal_evidence = {
        "gross_amount": {"currency_code": "USD", "value": "10.00"},
        "paypal_fee": {"currency_code": "USD", "value": "0.84"},
        "net_amount": {"currency_code": "USD", "value": "9.16"},
        "payer_country": "US",
    }
    save_results(run_id, {"run_id": run_id, "cases": [case.model_dump()]})

    create_calls: list[Case] = []
    capture_calls: list[Case] = []
    reconcile_calls: list[Case] = []

    def fake_create(*args: object, **kwargs: object) -> dict[str, Any]:
        create_calls.append(args[0])  # type: ignore[index]
        return {}

    def fake_capture(*args: object, **kwargs: object) -> None:
        capture_calls.append(args[0])  # type: ignore[index]
        return None

    def fake_reconcile_case(*args: object, **kwargs: object) -> dict[str, Any]:
        reconcile_calls.append(args[0])  # type: ignore[index]
        return args[0].model_dump()  # type: ignore[index]

    monkeypatch.setattr("paypal_sandbox_validation.cli._create_order", fake_create)
    monkeypatch.setattr("paypal_sandbox_validation.cli._capture", fake_capture)
    monkeypatch.setattr("paypal_sandbox_validation.cli._reconcile_case", fake_reconcile_case)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--accounts-csv", str(path), "--resume", run_id, "--merchant", "US", "--buyer", "US"]
    )
    assert result.exit_code == 0, result.output
    assert create_calls == []
    assert capture_calls == []
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0].case_id == case.case_id


def test_resume_after_approval_uses_existing_capture_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path)
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products
    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

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
    run_id = "resume-approval-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    case = plan[0]
    case.quote = adapter.build_quote("US", "US", "10.00", "USD")
    case.status = CaseStatus.BUYER_APPROVED
    case.order_id = "DUMMYORDER"
    case.approval_url = "https://www.sandbox.paypal.com/checkoutnow?token=DUMMYORDER"
    case.request_id_create = "req-create"
    case.request_id_capture = "req-capture-existing"
    save_results(run_id, {"run_id": run_id, "cases": [case.model_dump()]})

    create_calls: list[Case] = []
    approve_calls: list[Case] = []
    captured_request_ids: list[str | None] = []

    def fake_create(*args: object, **kwargs: object) -> None:
        create_calls.append(args[0])  # type: ignore[index]
        return None

    def fake_approve(*args: object, **kwargs: object) -> None:
        approve_calls.append(args[0])  # type: ignore[index]
        return None

    def fake_capture(case_arg: Case, merchant: Account, oauth_cache: object) -> dict[str, Any]:
        captured_request_ids.append(case_arg.request_id_capture)
        case_arg.status = CaseStatus.CAPTURED
        case_arg.paypal_evidence = {
            "gross_amount": {"currency_code": "USD", "value": "10.00"},
            "paypal_fee": {"currency_code": "USD", "value": "0.84"},
            "net_amount": {"currency_code": "USD", "value": "9.16"},
            "payer_country": "US",
        }
        return case_arg.model_dump()

    monkeypatch.setattr("paypal_sandbox_validation.cli._create_order", fake_create)
    monkeypatch.setattr("paypal_sandbox_validation.cli._approve_order", fake_approve)
    monkeypatch.setattr("paypal_sandbox_validation.cli._capture", fake_capture)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--accounts-csv", str(path), "--resume", run_id, "--merchant", "US", "--buyer", "US"]
    )
    assert result.exit_code == 0, result.output
    assert create_calls == []
    assert approve_calls == []
    assert captured_request_ids == ["req-capture-existing"]


def test_duplicate_payment_prevention_no_second_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path)
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products
    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

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
    run_id = "duplicate-prevention-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    case = plan[0]
    case.quote = adapter.build_quote("US", "US", "10.00", "USD")
    case.status = CaseStatus.BUYER_APPROVED
    case.order_id = "DUMMYORDER"
    case.approval_url = "https://www.sandbox.paypal.com/checkoutnow?token=DUMMYORDER"
    case.request_id_create = "req-create-existing"
    case.request_id_capture = "req-capture-existing"
    save_results(run_id, {"run_id": run_id, "cases": [case.model_dump()]})

    create_calls: list[Case] = []
    approve_calls: list[Case] = []

    def fake_create(case_arg: Case, *args: object, **kwargs: object) -> dict[str, Any]:
        create_calls.append(case_arg)
        case_arg.status = CaseStatus.ORDER_CREATED
        return case_arg.model_dump()

    def fake_approve(case_arg: Case, *args: object, **kwargs: object) -> None:
        approve_calls.append(case_arg)
        return None

    def fake_capture(case_arg: Case, *args: object, **kwargs: object) -> dict[str, Any]:
        case_arg.status = CaseStatus.CAPTURED
        return case_arg.model_dump()

    monkeypatch.setattr("paypal_sandbox_validation.cli._create_order", fake_create)
    monkeypatch.setattr("paypal_sandbox_validation.cli._approve_order", fake_approve)
    monkeypatch.setattr("paypal_sandbox_validation.cli._capture", fake_capture)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--accounts-csv", str(path), "--resume", run_id, "--merchant", "US", "--buyer", "US"]
    )
    assert result.exit_code == 0, result.output
    assert create_calls == []
    assert approve_calls == []


def test_observed_payer_country_populated_by_cli_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _us_only_csv(tmp_path)
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products
    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

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
    run_id = "observed-country-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    case = plan[0]
    case.quote = adapter.build_quote("US", "US", "10.00", "USD")
    case.status = CaseStatus.BUYER_APPROVED
    case.order_id = "DUMMYORDER"
    case.approval_url = "https://www.sandbox.paypal.com/checkoutnow?token=DUMMYORDER"
    case.request_id_create = "req-create"
    case.request_id_capture = "req-capture"
    save_results(run_id, {"run_id": run_id, "cases": [case.model_dump()]})

    def fake_capture(case_arg: Case, merchant: Account, oauth_cache: object) -> None:
        case_arg.status = CaseStatus.CAPTURED
        case_arg.observed_payer_country = "CA"
        case_arg.paypal_evidence = {
            "gross_amount": {"currency_code": "USD", "value": "10.00"},
            "paypal_fee": {"currency_code": "USD", "value": "0.84"},
            "net_amount": {"currency_code": "USD", "value": "9.16"},
            "payer_country": "CA",
        }
        return None

    monkeypatch.setattr("paypal_sandbox_validation.cli._create_order", lambda *a, **k: None)
    monkeypatch.setattr("paypal_sandbox_validation.cli._approve_order", lambda *a, **k: None)
    monkeypatch.setattr("paypal_sandbox_validation.cli._capture", fake_capture)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", "--accounts-csv", str(path), "--resume", run_id, "--merchant", "US", "--buyer", "US"]
    )
    assert result.exit_code == 0, result.output
    # The mismatch should be surfaced in the summary because buyer_country=US and observed=CA.
    assert "buyer_country_mismatches" in result.output


def test_api_failure_summary_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "api-failure-summary-001"
    monkeypatch.setattr("paypal_sandbox_validation.persistence.artifact_root", lambda: tmp_path)

    save_plan(run_id, [])
    summary = build_summary(run_id)
    assert summary["planned"] == 0
    assert summary["api_failures"] == 0

    save_results(
        run_id,
        {
            "run_id": run_id,
            "cases": [
                {"case_id": "c1", "status": "failed", "reconciliation": {"status": "paypal_api_failure"}},
                {"case_id": "c2", "status": "failed", "reconciliation": {"status": "authentication_failed"}},
                {"case_id": "c3", "status": "failed", "reconciliation": {"status": "account_configuration_difference"}},
                {"case_id": "c4", "status": "failed", "reconciliation": {}},
            ],
        },
    )
    summary = build_summary(run_id)
    assert summary["planned"] == 0
    assert summary["api_failures"] == 3  # paypal_api_failure, authentication_failed, fallback for missing rec_status
    assert summary["configuration_exclusions"] == 1
    assert summary["matches"] == 0


def _smoke_stop_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, continue_after_mismatch: bool
) -> tuple[list[str], dict[str, Any]]:
    path = _two_country_csv(tmp_path)
    from paypal_sandbox_validation.planner import build_plan, enrich_plan_with_products
    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

    monkeypatch.setattr("paypal_sandbox_validation.cli.ensure_surcharge_case", lambda plan, adapter: plan)

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
                "buyers_per_merchant": ["US", "CA"],
                "amount": "10.00",
                "currencies": {"US": "USD"},
            }
        },
    }
    run_id = f"smoke-{'continue' if continue_after_mismatch else 'stop'}-001"
    plan = build_plan(run_id=run_id, profile_name="smoke", scenarios=scenarios)
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)

    call_order: list[str] = []

    def fake_create(case_arg: Case, *args: object, **kwargs: object) -> None:
        call_order.append("create")
        case_arg.status = CaseStatus.ORDER_CREATED
        case_arg.order_id = f"ORDER-{case_arg.case_id}"
        case_arg.approval_url = f"https://www.sandbox.paypal.com/checkoutnow?token=ORDER-{case_arg.case_id}"
        return None

    def fake_approve(case_arg: Case, *args: object, **kwargs: object) -> None:
        call_order.append("approve")
        case_arg.status = CaseStatus.BUYER_APPROVED
        return None

    def fake_capture(case_arg: Case, *args: object, **kwargs: object) -> dict[str, Any]:
        call_order.append("capture")
        case_arg.status = CaseStatus.FAILED
        case_arg.reconciliation = {"status": "fee_mismatch"}
        return case_arg.model_dump()

    monkeypatch.setattr("paypal_sandbox_validation.cli._create_order", fake_create)
    monkeypatch.setattr("paypal_sandbox_validation.cli._approve_order", fake_approve)
    monkeypatch.setattr("paypal_sandbox_validation.cli._capture", fake_capture)

    args = ["run", "--accounts-csv", str(path), "--profile", "smoke"]
    if continue_after_mismatch:
        args.append("--continue-after-mismatch")
    runner = CliRunner()
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    start = result.output.find("{")
    assert start != -1
    summary = json.loads(result.output[start:])
    return call_order, summary


def test_smoke_stops_on_first_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order, summary = _smoke_stop_scenario(tmp_path, monkeypatch, continue_after_mismatch=False)
    assert summary["stopped_after_first_mismatch"] is True
    assert call_order.count("create") == 1
    assert call_order.count("capture") == 1


def test_explicit_continue_mode_processes_both_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_order, summary = _smoke_stop_scenario(tmp_path, monkeypatch, continue_after_mismatch=True)
    assert summary["stopped_after_first_mismatch"] is False
    assert call_order.count("create") == 2
    assert call_order.count("capture") == 2


def test_classify_invalid_client_as_authentication_failed() -> None:
    exc = PayPalAPIError(
        "invalid_client",
        status_code=401,
        body={"error": "invalid_client", "error_description": "Client Authentication failed"},
        operation="create order",
    )
    from paypal_sandbox_validation.error_classification import classify_paypal_api_error

    status, safe, detail = classify_paypal_api_error(exc)
    assert status == ReconciliationStatus.AUTHENTICATION_FAILED
    assert safe["error"] == "invalid_client"
    assert "create order" in detail
