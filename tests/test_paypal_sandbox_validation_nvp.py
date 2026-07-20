from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from paypal_sandbox_validation.accounts import (
    EXPECTED_HEADERS,
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.cli import cli
from paypal_sandbox_validation.models import Account, AccountType, CaseStatus
from paypal_sandbox_validation.nvp import (
    NVPInvalidHostError,
    NVPResponse,
    PayPalNVPClient,
    extract_transaction_details,
    extract_transaction_search_results,
    find_unique_transaction,
    poll_for_unique_transaction,
)

VALID_HEADER = "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret;nvp_user;nvp_password;nvp_signature"  # noqa: E501


def _tmp_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(content, encoding="utf-8")
    return path


def _nvp_response(raw: dict[str, Any]) -> NVPResponse:
    """Build an NVPResponse with both alias fields and raw storage."""
    return NVPResponse(raw=raw, **raw)


def test_expected_csv_headers_exact() -> None:
    assert EXPECTED_HEADERS == [
        "country_code",
        "account_type",
        "primary_email_alias",
        "password",
        "first_name",
        "last_name",
        "ppBalance",
        "addBank",
        "ccType",
        "payment_card",
        "client_id",
        "secret",
        "nvp_user",
        "nvp_password",
        "nvp_signature",
    ]


def test_csv_without_nvp_columns_rejected(tmp_path: Path) -> None:
    path = _tmp_csv(
        tmp_path,
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret\n"
        "US;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB\n",
    )
    with pytest.raises(ValueError, match="headers do not match expected schema"):
        parse_accounts_csv(path)


def test_business_requires_nvp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "US;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB;nvpu;nvpp;\n",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    with pytest.raises(ValueError, match="Missing Business NVP credentials"):
        parse_accounts_csv(path)


def test_personal_nvp_fields_must_be_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "US;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB;nvpu;nvpp;nvps\n"
        "US;PERSONAL;b@p.com;secret2;Test;Buyer;99999;Y;VISA;4111111111111111;;;nvpu;nvpp;nvps\n",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    with pytest.raises(ValueError, match="Business NVP credentials on Personal row"):
        parse_accounts_csv(path)


def test_duplicate_nvp_user_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "US;BUSINESS;m1@b.com;secret1;Test;Merchant1;99999;Y;VISA;4111111111111111;AAAA;BBBB;same-nvp;nvp-pwd1;sig1\n"
        "CA;BUSINESS;m2@b.com;secret2;Test;Merchant2;99999;Y;VISA;4111111111111111;CCCC;DDDD;same-nvp;nvp-pwd2;sig2\n"
        "US;PERSONAL;b@p.com;secret3;Test;Buyer;99999;Y;VISA;4111111111111111;;;;;\n",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US", "CA"})
    result = validate_accounts(parse_accounts_csv(path))
    assert result["valid"] is False
    assert "same-nvp" in result["duplicate_nvp_users"]


def test_incomplete_nvp_triple_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "US;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB;nvpu;;nvps\n"
        "US;PERSONAL;b@p.com;secret2;Test;Buyer;99999;Y;VISA;4111111111111111;;;;;\n",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    with pytest.raises(ValueError, match="Missing Business NVP credentials"):
        parse_accounts_csv(path)


def test_account_model_excludes_nvp_from_public_dump() -> None:
    account = Account(
        country_code="US",
        account_type=AccountType.BUSINESS,
        primary_email_alias="m@b.com",
        password="secret",
        first_name="Test",
        last_name="Merchant",
        client_id="AAAA",
        secret="BBBB",
        nvp_user="nvpu",
        nvp_password="nvpp",
        nvp_signature="nvps",
    )
    dumped = account.model_dump()
    assert "nvp_user" not in dumped
    assert "nvp_password" not in dumped
    assert "nvp_signature" not in dumped
    assert "password" not in dumped
    assert "secret" not in dumped
    assert "client_id" not in dumped
    assert "nvp" not in repr(account).lower()


def test_nvp_url_encoding_no_body_logging() -> None:
    from paypal_sandbox_validation.nvp import NVPRequest

    request = NVPRequest(
        "TransactionSearch",
        {"USER": "u", "PWD": "p", "SIGNATURE": "s", "METHOD": "TransactionSearch"},
    )
    body = request.to_body()
    assert b"USER=u" in body
    assert b"PWD=p" in body
    assert b"SIGNATURE=s" in body
    assert b"METHOD=TransactionSearch" in body


def test_nvp_client_enforces_sandbox_host() -> None:
    account = Account(
        country_code="US",
        account_type=AccountType.BUSINESS,
        primary_email_alias="m@b.com",
        password="secret",
        first_name="Test",
        last_name="Merchant",
        nvp_user="u",
        nvp_password="p",
        nvp_signature="s",
    )
    with pytest.raises(NVPInvalidHostError):
        PayPalNVPClient(account, endpoint="https://api-3t.paypal.com/nvp")


def test_transaction_search_parsing_unique_and_missing() -> None:
    raw = {
        "ACK": "Success",
        "L_TRANSACTIONID0": "TXN1",
        "L_TIMESTAMP0": "2026-07-19T12:00:00Z",
        "L_TYPE0": "Payment",
        "L_EMAIL0": "buyer@example.com",
        "L_NAME0": "Buyer",
        "L_STATUS0": "Completed",
        "L_AMT0": "1.00",
        "L_CURRENCYCODE0": "EUR",
        "L_FEEAMT0": "-0.37",
        "L_NETAMT0": "0.63",
    }
    response = _nvp_response(raw)
    results = extract_transaction_search_results(response)
    assert len(results) == 1
    assert results[0]["transaction_id"] == "TXN1"

    search = find_unique_transaction(response, "1.00", "EUR", "buyer@example.com")
    assert search["status"] == "found"
    assert search["transaction"]["transaction_id"] == "TXN1"

    missing = find_unique_transaction(response, "9.99", "EUR")
    assert missing["status"] == "nvp_transaction_not_found"


def test_transaction_search_detects_ambiguous_match() -> None:
    raw = {
        "ACK": "Success",
        "L_TRANSACTIONID0": "TXN1",
        "L_TIMESTAMP0": "2026-07-19T12:00:00Z",
        "L_TYPE0": "Payment",
        "L_EMAIL0": "buyer@example.com",
        "L_NAME0": "Buyer",
        "L_STATUS0": "Completed",
        "L_AMT0": "1.00",
        "L_CURRENCYCODE0": "EUR",
        "L_FEEAMT0": "-0.37",
        "L_NETAMT0": "0.63",
        "L_TRANSACTIONID1": "TXN2",
        "L_TIMESTAMP1": "2026-07-19T12:01:00Z",
        "L_TYPE1": "Payment",
        "L_EMAIL1": "buyer@example.com",
        "L_NAME1": "Buyer",
        "L_STATUS1": "Completed",
        "L_AMT1": "1.00",
        "L_CURRENCYCODE1": "EUR",
        "L_FEEAMT1": "-0.37",
        "L_NETAMT1": "0.63",
    }
    response = _nvp_response(raw)
    ambiguous = find_unique_transaction(response, "1.00", "EUR")
    assert ambiguous["status"] == "nvp_transaction_ambiguous"


def test_get_transaction_details_parsing_and_missing_fee() -> None:
    raw_complete = {
        "ACK": "Success",
        "TRANSACTIONTYPE": "sendmoney",
        "PAYMENTTYPE": "instant",
        "ORDERTIME": "2026-07-19T12:00:00Z",
        "AMT": "1.00",
        "FEEAMT": "0.37",
        "CURRENCYCODE": "EUR",
        "PAYMENTSTATUS": "Completed",
        "COUNTRYCODE": "DE",
    }
    details = extract_transaction_details(_nvp_response(raw_complete))
    assert details is not None
    assert details["transaction_type"] == "sendmoney"
    assert details["payment_type"] == "instant"
    assert Decimal(details["fee_amt"]) == Decimal("0.37")
    assert details["has_fee"] is True
    assert details["country_code"] == "DE"

    raw_missing_fee = {
        "ACK": "Success",
        "TRANSACTIONTYPE": "sendmoney",
        "AMT": "1.00",
        "CURRENCYCODE": "EUR",
        "PAYMENTSTATUS": "Completed",
        "COUNTRYCODE": "DE",
    }
    details_missing = extract_transaction_details(_nvp_response(raw_missing_fee))
    assert details_missing is not None
    assert details_missing["has_fee"] is False
    assert details_missing["fee_amt"] is None


def test_fx_exclusion_detected() -> None:
    from paypal_sandbox_validation.manual_flow import _detect_fx

    fx_details = {
        "exchange_rate": "1.12",
        "currency_code": "EUR",
        "settle_amt": None,
        "settle_currency": None,
    }
    assert _detect_fx(fx_details) is True

    non_fx = {
        "exchange_rate": None,
        "settle_amt": None,
        "settle_currency": None,
        "currency_code": "EUR",
    }
    assert _detect_fx(non_fx) is False


def test_buyer_country_verification_from_nvp() -> None:
    from paypal_sandbox_validation.reconciliation import reconcile

    quote = {
        "provider": "paypal",
        "processing_fee": {"value": "0.37", "currency": "EUR"},
        "net_amount": {"value": "0.63", "currency": "EUR"},
        "components": [],
        "matched_rules": [],
    }
    evidence = {
        "gross_amount": {"value": "1.00", "currency_code": "EUR"},
        "paypal_fee": {"value": "0.37", "currency_code": "EUR"},
        "net_amount": {"value": "0.63", "currency_code": "EUR"},
        "payer_country": "DE",
    }
    rec = reconcile(
        paypal_evidence=evidence,
        quote=quote,
        merchant_country="DE",
        buyer_country="DE",
        observed_payer_country="DE",
    )
    assert rec.status.value == "match"


def test_resume_search_before_resend_does_not_call_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a transaction ID is already persisted, _fetch_details is used; Playwright is not invoked."""
    from paypal_sandbox_validation import manual_flow

    merchant = Account(
        country_code="DE",
        account_type=AccountType.BUSINESS,
        primary_email_alias="m@b.com",
        password="secret",
        first_name="Test",
        last_name="Merchant",
        nvp_user="u",
        nvp_password="p",
        nvp_signature="s",
    )
    buyer = Account(
        country_code="DE",
        account_type=AccountType.PERSONAL,
        primary_email_alias="buyer@p.com",
        password="secret2",
        first_name="Test",
        last_name="Buyer",
    )

    class FakeCase:
        def __init__(self, quote: dict[str, Any]) -> None:
            self.case_id = "c1"
            self.run_id = "r1"
            self.status = CaseStatus.PLANNED
            self.merchant_country = "DE"
            self.buyer_country = "DE"
            self.amount = "1.00"
            self.currency = "EUR"
            self.execution_path = "manual_send_to_business"
            self.product_id = "goods_and_services"
            self.variant_id = "standard"
            self.manual_payment_type = None
            self.funding_source = None
            self.evidence_source = None
            self.paypal_evidence = None
            self.quote = quote
            self.reconciliation = None
            self.paypal_error = None
            self.paypal_issue = None
            self.manual_state = None
            self.manual_submitted_at = None
            self.buyer_ui_evidence = None
            self.nvp_transaction_id = None
            self.pilot_metadata: dict[str, Any] = {}
            self.product_selection_source = "explicit_execution_path_mapping"
            self.prediction_provenance = "pre_submission_prediction"
            self.prediction_created_before_original_submission = True
            self.prediction_created_before_observation_reuse = True
            self.original_submission_timestamp_known = False
            self.prediction_sha256 = None
            self.prediction_created_at = None
            self.prediction_unchanged_after_observation = None

    class FakeBrowser:
        called = False

        def send_payment(self, *args, **kwargs) -> dict[str, Any]:
            FakeBrowser.called = True
            return {"status": "submitted", "submitted_at": "2026-07-19T12:00:00+00:00"}

    def fake_fetch(*args, **kwargs):
        return {
            "status": "found",
            "details": {
                "transaction_type": "sendmoney",
                "payment_type": "instant",
                "amt": "1.00",
                "fee_amt": "0.37",
                "currency_code": "EUR",
                "payment_status": "Completed",
                "country_code": "DE",
                "exchange_rate": None,
                "settle_amt": None,
                "settle_currency": None,
            },
        }

    def fake_private_state(*args, **kwargs):
        return {"c1": "TXN1"}

    monkeypatch.setattr(manual_flow, "_fetch_details", lambda *a, **k: fake_fetch())
    monkeypatch.setattr(manual_flow, "load_manual_private_state", fake_private_state)
    monkeypatch.setattr(manual_flow, "save_manual_private_state", lambda *a, **k: None)
    monkeypatch.setattr(manual_flow, "validate_case_constraints", lambda c: {"valid": True})

    from paypal_sandbox_validation.quote_adapter import QuoteAdapter

    adapter = QuoteAdapter()
    quote = adapter.build_quote("DE", "DE", "1.00", "EUR", product_id="goods_and_services", variant_id="standard")
    browser = FakeBrowser()
    fake_case = FakeCase(quote)
    manual_flow._set_prediction(fake_case, quote)
    returned = manual_flow.run_manual_case(fake_case, buyer, merchant, adapter, browser)  # type: ignore[arg-type]
    assert FakeBrowser.called is False
    assert returned.reconciliation is not None
    assert returned.reconciliation["status"] == "match"
    assert returned.pilot_metadata["duplicate_prevention"] == "resumed_from_private_state"
    assert returned.prediction_unchanged_after_observation is True


def test_no_business_ui_method_in_manual_browser() -> None:
    from paypal_sandbox_validation.manual_browser import ManualPaymentBrowser

    assert not hasattr(ManualPaymentBrowser, "find_merchant_transaction")


def test_probe_nvp_command_requires_complete_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "US;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB;nvpu;nvpp;nvps\n"
        "US;PERSONAL;b@p.com;secret2;Test;Buyer;99999;Y;VISA;4111111111111111;;;;;\n",
    )
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"US"})
    monkeypatch.setattr(
        "paypal_sandbox_validation.nvp.PayPalNVPClient.transaction_search",
        lambda *a, **k: _nvp_response({"ACK": "Success"}),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["probe-nvp", "--accounts-csv", str(path)])
    assert result.exit_code == 0
    assert "merchants_selected = 1" in result.output
    assert "nvp_credentials_valid = 1" in result.output


def test_nvp_poll_for_unique_transaction_found() -> None:
    """poll_for_unique_transaction returns the first found result and skips remaining attempts."""
    raw = {
        "ACK": "Success",
        "L_TRANSACTIONID0": "TXN1",
        "L_TIMESTAMP0": "2026-07-19T12:00:00Z",
        "L_TYPE0": "Payment",
        "L_EMAIL0": "buyer@example.com",
        "L_NAME0": "Buyer",
        "L_STATUS0": "Completed",
        "L_AMT0": "1.00",
        "L_CURRENCYCODE0": "EUR",
        "L_FEEAMT0": "-0.37",
        "L_NETAMT0": "0.63",
    }

    class FakeClient:
        calls = 0

        def transaction_search(self, *args, **kwargs):
            FakeClient.calls += 1
            return _nvp_response(raw)

    result = poll_for_unique_transaction(
        FakeClient(),  # type: ignore[arg-type]
        start_date="2026-07-19T00:00:00Z",
        end_date="2026-07-19T23:59:59Z",
        amount="1.00",
        currency="EUR",
        buyer_email="buyer@example.com",
        max_attempts=3,
        delay_seconds=0.0,
    )
    assert result["status"] == "found"
    assert result["transaction"]["transaction_id"] == "TXN1"
    assert FakeClient.calls == 1
