from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner
from paypal_sandbox_validation.cli import cli
from paypal_sandbox_validation.models import Account, AccountType, Case, CaseStatus, QualificationStatus
from paypal_sandbox_validation.profile_pricing import (
    ALLOWED_HOSTS,
    PROFILE_PAGE_PATH,
    build_profile_pricing_verifications,
    extract_profile_pricing_from_html,
    record_profile_pricing,
    validate_profile_pricing_input,
    verify_profile_against_transactions,
)
from paypal_sandbox_validation.qualification import promote_observation_fixtures

SAMPLE_DE_HTML = """
<html><body>
  <h2>When customers pay with PayPal:</h2>
  <p>1.90% + EUR 0.35 per transaction</p>
  <h2>When customers pay with credit or debit card:</h2>
  <p>Starting at 2.99% + EUR 0.39 per transaction</p>
</body></html>
"""

SAMPLE_AU_HTML = """
<html><body>
  <h2>When customers pay with PayPal:</h2>
  <p>2.40% + AUD 0.30 per transaction</p>
  <h2>When customers pay with credit or debit card:</h2>
  <p>Starting at 1.75% + AUD 0.30 per transaction</p>
</body></html>
"""


def _de_account() -> Account:
    return Account(
        country_code="DE",
        account_type=AccountType.BUSINESS,
        primary_email_alias="de-merchant@example.com",
        password="x",
        first_name="Test",
        last_name="Merchant",
    )


def test_validate_profile_pricing_input_accepts_valid_de() -> None:
    evidence = validate_profile_pricing_input(
        merchant_country="DE",
        wallet_percentage="1.90",
        wallet_fixed="0.35",
        wallet_currency="EUR",
        card_percentage="2.99",
        card_fixed="0.39",
        card_currency="EUR",
        card_qualifier="starting_at",
    )
    assert evidence.wallet.percentage == "1.90"
    assert evidence.wallet.fixed == {"value": "0.35", "currency": "EUR"}
    assert evidence.card.qualifier == "starting_at"


def test_validate_profile_pricing_input_rejects_unsupported_qualifier() -> None:
    with pytest.raises(ValueError, match="Unsupported card qualifier"):
        validate_profile_pricing_input(
            merchant_country="DE",
            wallet_percentage="1.90",
            wallet_fixed="0.35",
            wallet_currency="EUR",
            card_percentage="2.99",
            card_fixed="0.39",
            card_currency="EUR",
            card_qualifier="from",
        )


def test_validate_profile_pricing_input_rejects_bad_minor_units() -> None:
    with pytest.raises(ValueError, match="decimal places"):
        validate_profile_pricing_input(
            merchant_country="DE",
            wallet_percentage="1.90",
            wallet_fixed="0.355",
            wallet_currency="EUR",
            card_percentage="2.99",
            card_fixed="0.39",
            card_currency="EUR",
        )


def test_extract_profile_pricing_from_html_de() -> None:
    evidence = extract_profile_pricing_from_html(SAMPLE_DE_HTML, "DE", "EUR", "EUR")
    assert evidence.wallet.percentage == "1.90"
    assert evidence.wallet.fixed == {"value": "0.35", "currency": "EUR"}
    assert evidence.card.percentage == "2.99"
    assert evidence.card.qualifier == "starting_at"


def test_extract_profile_pricing_from_html_au() -> None:
    evidence = extract_profile_pricing_from_html(SAMPLE_AU_HTML, "AU", "AUD", "AUD")
    assert evidence.wallet.percentage == "2.40"
    assert evidence.wallet.fixed == {"value": "0.30", "currency": "AUD"}
    assert evidence.card.percentage == "1.75"
    assert evidence.card.qualifier == "starting_at"


def test_extract_profile_pricing_fail_closed_on_unknown_layout() -> None:
    with pytest.raises(ValueError, match="headings not found"):
        extract_profile_pricing_from_html("<html><body>random</body></html>", "DE", "EUR", "EUR")


def test_record_profile_pricing_marks_not_representative() -> None:
    registry: dict[str, object] = {}
    evidence = validate_profile_pricing_input(
        merchant_country="DE",
        wallet_percentage="1.90",
        wallet_fixed="0.35",
        wallet_currency="EUR",
        card_percentage="2.99",
        card_fixed="0.39",
        card_currency="EUR",
        card_qualifier="starting_at",
    )
    record_profile_pricing(registry, evidence, set_manual_send=True)
    entry = registry["DE"]
    assert entry["representative_for_public_rates"] is False
    assert entry["manual_send_to_business"] == QualificationStatus.SANDBOX_PROFILE_PRICING_CONFIRMED.value
    assert entry["sandbox_profile_pricing"]["wallet"]["percentage"] == "1.90"


def test_verify_profile_matches_transactions_de() -> None:
    registry = {
        "DE": {
            "merchant_country": "DE",
            "representative_for_public_rates": False,
            "sandbox_profile_pricing": {
                "wallet": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
            },
            "observed_account_formula": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
        }
    }
    result = verify_profile_against_transactions(registry, "DE")
    assert result.status == "profile_matches_transactions"
    assert result.delta_minor_units == 0
    assert result.production_representative is False


def test_verify_profile_mismatch_when_transactions_differ() -> None:
    registry = {
        "DE": {
            "merchant_country": "DE",
            "representative_for_public_rates": False,
            "sandbox_profile_pricing": {
                "wallet": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
            },
            "observed_account_formula": {"percentage": "2.40", "fixed": {"value": "0.35", "currency": "EUR"}},
        }
    }
    result = verify_profile_against_transactions(registry, "DE")
    assert result.status == "profile_transaction_mismatch"
    assert result.delta_minor_units != 0


def test_verify_profile_missing_evidence() -> None:
    registry = {"DE": {"merchant_country": "DE"}}
    assert verify_profile_against_transactions(registry, "DE").status == "profile_evidence_missing"


def test_verify_profile_missing_transaction() -> None:
    registry = {
        "DE": {
            "merchant_country": "DE",
            "sandbox_profile_pricing": {
                "wallet": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
            },
        }
    }
    assert verify_profile_against_transactions(registry, "DE").status == "transaction_evidence_missing"


def test_profile_pricing_does_not_promote_public_fixture(tmp_path: Path) -> None:
    case = Case(
        case_id="prof-DE-1",
        run_id="r1",
        merchant_country="DE",
        buyer_country="DE",
        amount="10.00",
        currency="EUR",
        product_id="paypal_checkout",
        variant_id="standard",
        status=CaseStatus.RECONCILED,
        execution_classification="sandbox_profile_pricing",
    )
    case.reconciliation = {"status": "match"}
    case.paypal_evidence = {
        "status": "COMPLETED",
        "gross_amount": {"value": "10.00", "currency_code": "EUR"},
        "paypal_fee": {"value": "0.54", "currency_code": "EUR"},
        "net_amount": {"value": "9.46", "currency_code": "EUR"},
        "payer_country": "DE",
    }
    fixtures_dir = tmp_path / "fixtures"
    diagnostics_dir = tmp_path / "diagnostics"
    paths = promote_observation_fixtures([case], fixtures_dir=fixtures_dir, diagnostics_dir=diagnostics_dir)
    positive_fixtures = [p for p in paths if p.parent.name != "diagnostics"]
    assert positive_fixtures == []


def test_exact_sandbox_url_allowlist() -> None:
    host = f"https://www.sandbox.paypal.com/{PROFILE_PAGE_PATH}"
    assert urlparse(host).hostname in ALLOWED_HOSTS
    assert "www.paypal.com" not in ALLOWED_HOSTS
    assert "sandbox.paypal.com" not in ALLOWED_HOSTS


def test_live_paypal_hostname_rejected() -> None:
    from paypal_sandbox_validation.profile_pricing import _validate_url

    with pytest.raises(ValueError, match="non-Sandbox"):
        _validate_url("https://www.paypal.com/merchantapps/businesstools/acceptpayments/checkout")


def test_profile_evidence_is_secret_free() -> None:
    evidence = validate_profile_pricing_input(
        merchant_country="AU",
        wallet_percentage="2.40",
        wallet_fixed="0.30",
        wallet_currency="AUD",
        card_percentage="1.75",
        card_fixed="0.30",
        card_currency="AUD",
        card_qualifier="starting_at",
    )
    data = evidence.model_dump()
    assert "provider" in data
    assert "environment" in data
    assert "password" not in json.dumps(data)
    assert "client_id" not in json.dumps(data)


def test_build_profile_pricing_verifications() -> None:
    registry = {
        "DE": {
            "merchant_country": "DE",
            "representative_for_public_rates": False,
            "sandbox_profile_pricing": {
                "wallet": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
            },
            "observed_account_formula": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
        }
    }
    verifications = build_profile_pricing_verifications(registry)
    assert len(verifications) == 1
    assert verifications[0]["status"] == "profile_matches_transactions"


def test_record_profile_pricing_cli(tmp_path: Path) -> None:
    csv = tmp_path / "accounts.csv"
    csv.write_text(
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret;nvp_user;nvp_password;nvp_signature\n"
        "DE;BUSINESS;de@example.com;secret;Test;Merchant;99999;Y;VISA;4111;cid;sec;nuser;npw;nsig\n"
    )
    registry_path = tmp_path / "registry.json"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "record-profile-pricing",
            "--merchant",
            "DE",
            "--wallet-percentage",
            "1.90",
            "--wallet-fixed",
            "0.35",
            "--wallet-currency",
            "EUR",
            "--card-percentage",
            "2.99",
            "--card-fixed",
            "0.39",
            "--card-currency",
            "EUR",
            "--card-qualifier",
            "starting_at",
            "--accounts-csv",
            str(csv),
            "--qualification-registry",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0, result.output
    registry = json.loads(registry_path.read_text())
    assert registry["DE"]["sandbox_profile_pricing"]["wallet"]["percentage"] == "1.90"
    assert registry["DE"]["representative_for_public_rates"] is False


def test_record_profile_pricing_cli_rejects_unknown_merchant(tmp_path: Path) -> None:
    csv = tmp_path / "accounts.csv"
    csv.write_text(
        "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret;nvp_user;nvp_password;nvp_signature\n"
        "US;BUSINESS;us@example.com;secret;Test;Merchant;99999;Y;VISA;4111;cid;sec;nuser;npw;nsig\n"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "record-profile-pricing",
            "--merchant",
            "DE",
            "--wallet-percentage",
            "1.90",
            "--wallet-fixed",
            "0.35",
            "--wallet-currency",
            "EUR",
            "--card-percentage",
            "2.99",
            "--card-fixed",
            "0.39",
            "--card-currency",
            "EUR",
            "--accounts-csv",
            str(csv),
        ],
    )
    assert result.exit_code != 0
