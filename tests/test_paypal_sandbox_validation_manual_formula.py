from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from paypal_sandbox_validation import manual_flow
from paypal_sandbox_validation.cli import _manual_consistency_checks
from paypal_sandbox_validation.models import Account, AccountType, Case, CaseStatus, ReconciliationStatus
from paypal_sandbox_validation.quote_adapter import QuoteAdapter

VALID_HEADER = "country_code;account_type;primary_email_alias;password;first_name;last_name;ppBalance;addBank;ccType;payment_card;client_id;secret;nvp_user;nvp_password;nvp_signature"  # noqa: E501


def _tmp_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(content, encoding="utf-8")
    return path


def _de_account_csv(tmp_path: Path) -> Path:
    return _tmp_csv(
        tmp_path,
        f"{VALID_HEADER}\n"
        "DE;BUSINESS;m@b.com;secret1;Test;Merchant;99999;Y;VISA;4111111111111111;AAAA;BBBB;nvpu;nvpp;nvps\n"
        "DE;PERSONAL;b@p.com;secret2;Test;Buyer;99999;Y;VISA;4111111111111111;;;;;\n",
    )


def _make_case_with_quote(
    amount: str = "1.00",
    product_id: str = "goods_and_services",
    variant_id: str = "standard",
) -> Case:
    adapter = QuoteAdapter()
    quote = adapter.build_quote("DE", "DE", amount, "EUR", product_id=product_id, variant_id=variant_id)
    case = Case(
        case_id=f"manual-DE-DE-{amount}-EUR-test",
        run_id="test-run",
        merchant_country="DE",
        buyer_country="DE",
        amount=amount,
        currency="EUR",
        execution_path="manual_send_to_business",
        product_id=product_id,
        variant_id=variant_id,
        status=CaseStatus.PREDICTION_READY,
    )
    manual_flow._set_prediction(case, quote)
    case.manual_submitted_at = "2026-07-20T12:00:00+00:00"
    return case


def _details(
    amount: str = "1.00",
    fee: str = "0.37",
    transaction_type: str = "sendmoney",
    payment_type: str = "instant",
    payment_status: str = "Completed",
    country_code: str = "DE",
) -> dict[str, Any]:
    return {
        "transaction_type": transaction_type,
        "payment_type": payment_type,
        "order_time": "2026-07-20T12:00:00Z",
        "amt": amount,
        "fee_amt": fee,
        "currency_code": "EUR",
        "payment_status": payment_status,
        "country_code": country_code,
        "exchange_rate": None,
        "settle_amt": None,
        "settle_currency": None,
    }


def test_product_selected_before_submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan phase selects product/variant from the execution-path mapping, never from a fee."""
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"DE"})
    accounts_csv = _de_account_csv(tmp_path)
    plan = manual_flow.build_manual_plan("run-1", "manual-de-formula", str(accounts_csv), [("DE", "DE", "1.00", "EUR")])
    assert len(plan) == 1
    case = plan[0]
    assert case.status == CaseStatus.PREDICTION_READY
    assert case.product_id == "goods_and_services"
    assert case.variant_id == "standard"
    assert case.product_selection_source == "explicit_execution_path_mapping"
    assert case.prediction_provenance == "pre_submission_prediction"
    assert case.prediction_created_before_original_submission is True
    assert case.prediction_created_before_observation_reuse is True
    assert case.original_submission_timestamp_known is False
    assert case.prediction_sha256 is not None
    assert case.prediction_created_at is not None
    assert case.quote is not None
    assert case.quote["processing_fee"]["value"] == "0.37"


def test_observed_fee_cannot_change_product() -> None:
    """When other_commercial would match the observed fee, the fixed goods_and_services quote is still used."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    # Observed fee 0.42 matches DE other_commercial/standard, not goods_and_services/standard.
    details = _details(fee="0.42")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.product_id == "goods_and_services"
    assert returned.variant_id == "standard"
    assert returned.paypal_issue == ReconciliationStatus.FEE_MISMATCH.value
    assert returned.prediction_unchanged_after_observation is True


def test_inverse_fixture_other_commercial_fixed() -> None:
    """When the mapping is other_commercial and the observed fee matches goods_and_services, still fail."""
    # Build an other_commercial quote (0.42 for EUR 1.00) and observe 0.37.
    case = _make_case_with_quote("1.00", "other_commercial", "standard")
    adapter = QuoteAdapter()
    details = _details(fee="0.37")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.product_id == "other_commercial"
    assert returned.variant_id == "standard"
    assert returned.paypal_issue == ReconciliationStatus.FEE_MISMATCH.value


def test_prediction_hash_unchanged() -> None:
    """A matching observation must leave the prediction hash unchanged."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    original_hash = case.prediction_sha256
    adapter = QuoteAdapter()
    returned = manual_flow._run_reconciliation(case, _details(), adapter)
    assert returned.status == CaseStatus.RECONCILED
    assert returned.prediction_sha256 == original_hash
    assert returned.prediction_unchanged_after_observation is True


def test_prediction_hash_changes_when_quote_mutated() -> None:
    """Mutating the quote after submission must be detected and fail the case."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    # Mutate the stored quote processing fee to simulate tampering.
    case.quote["processing_fee"]["value"] = "9.99"
    adapter = QuoteAdapter()
    returned = manual_flow._run_reconciliation(case, _details(), adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.paypal_issue == ReconciliationStatus.PREDICTION_CHANGED.value


def test_transaction_type_mismatch() -> None:
    """A transaction type other than sendmoney must fail with transaction_type_mismatch."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    details = _details(transaction_type="payment")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.paypal_issue == ReconciliationStatus.TRANSACTION_TYPE_MISMATCH.value


def test_unsupported_payment_type() -> None:
    """PAYMENTTYPE values outside the configured supported list must fail."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    details = _details(payment_type="echeck")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.paypal_issue == ReconciliationStatus.UNSUPPORTED_PAYMENT_TYPE.value


def test_incomplete_payment_rejected() -> None:
    """Only Completed payments are accepted as fee evidence."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    details = _details(payment_status="Pending")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.paypal_issue == ReconciliationStatus.INCOMPLETE_PAYMENT.value


def test_buyer_country_mismatch_from_nvp() -> None:
    """COUNTRYCODE from NVP must match the configured buyer country."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    details = _details(country_code="US")
    returned = manual_flow._run_reconciliation(case, details, adapter)
    assert returned.status == CaseStatus.FAILED
    assert returned.paypal_issue == ReconciliationStatus.BUYER_COUNTRY_MISMATCH.value


def test_existing_transaction_reused_without_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing NVP transaction must be reused and Playwright must not be invoked."""
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

    class FakeBrowser:
        called = False

        def send_payment(self, *args, **kwargs) -> dict[str, Any]:
            FakeBrowser.called = True
            return {"status": "submitted", "submitted_at": "2026-07-20T12:00:00+00:00"}

    def fake_fetch(*args, **kwargs):
        return {
            "status": "found",
            "details": _details(),
        }

    monkeypatch.setattr(
        manual_flow,
        "_find_persisted_reusable_case",
        lambda case: {
            "transaction_id": "TXN1",
            "manual_submitted_at": "2026-07-20T12:00:00Z",
        },
    )
    monkeypatch.setattr(manual_flow, "_fetch_details", fake_fetch)
    monkeypatch.setattr(manual_flow, "save_manual_private_state", lambda *a, **k: None)
    monkeypatch.setattr(manual_flow, "validate_case_constraints", lambda c: {"valid": True})

    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    browser = FakeBrowser()
    returned = manual_flow.run_manual_case(case, buyer, merchant, adapter, browser)
    assert FakeBrowser.called is False
    assert returned.status == CaseStatus.RECONCILED
    assert returned.pilot_metadata["duplicate_prevention"] == "existing_transaction_reused"
    assert returned.nvp_transaction_id == "TXN1"
    assert returned.prediction_provenance == "historical_observation_requoted"
    assert returned.prediction_created_before_original_submission is False
    assert returned.prediction_created_before_observation_reuse is True
    assert returned.original_submission_timestamp_known is True
    assert returned.prediction_unchanged_after_observation is True


def test_formula_inference_from_three_observations() -> None:
    """infer_formula returns one shared product/variant and observations for all three amounts."""
    cases = [
        _build_reconciled_case("1.00", "0.37"),
        _build_reconciled_case("10.00", "0.60"),
        _build_reconciled_case("100.00", "2.84"),
    ]
    formula = manual_flow.infer_formula(cases)
    assert formula is not None
    assert formula["product_id"] == "goods_and_services"
    assert formula["variant_id"] == "standard"
    assert formula["base_percentage"] == "2.49"
    assert formula["fixed_amount"] == "0.35"
    assert len(formula["inferred_from_observations"]["observations"]) == 3
    assert formula["inferred_from_observations"]["base_percentage"]
    assert formula["inferred_from_observations"]["fixed_amount"]


def test_manual_consistency_check_for_historical_observation() -> None:
    """The EUR 1.00 historical observation must match the formula inferred from 10/100."""
    formula = {
        "product_id": "goods_and_services",
        "variant_id": "standard",
        "base_percentage": "2.49",
        "fixed_amount": "0.35",
        "inferred_from_observations": {
            "base_percentage": "0.0190",
            "fixed_amount": "0.3506",
            "observations": [
                {"amount": "10.00", "paypal_fee": "0.54", "library_fee": "0.60"},
                {"amount": "100.00", "paypal_fee": "2.25", "library_fee": "2.84"},
            ],
        },
    }
    historical = _make_case_with_quote("1.00", "goods_and_services", "standard")
    manual_flow._set_historical_requoted_prediction(historical, "2026-07-20T12:00:00Z")
    adapter = QuoteAdapter()
    manual_flow._run_reconciliation(historical, _details(amount="1.00", fee="0.37"), adapter)
    checks = _manual_consistency_checks([historical], formula)
    assert len(checks) == 1
    assert checks[0]["amount"] == "1.00"
    assert checks[0]["observed_paypal_fee"] == "0.37"
    assert checks[0]["expected_paypal_fee"] == "0.37"
    assert checks[0]["matches"] is True


def test_historical_observation_cannot_claim_pre_submission_prediction() -> None:
    """A reused historical observation must be labelled as re-quoted at reuse time."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    manual_flow._set_historical_requoted_prediction(case, "2026-07-20T12:00:00Z")
    adapter = QuoteAdapter()
    returned = manual_flow._run_reconciliation(case, _details(), adapter)
    assert returned.status == CaseStatus.RECONCILED
    assert returned.prediction_provenance == "historical_observation_requoted"
    assert returned.prediction_created_before_original_submission is False
    assert returned.prediction_created_before_observation_reuse is True
    assert returned.original_submission_timestamp_known is True
    assert returned.prediction_unchanged_after_observation is True


def _build_reconciled_case(amount: str, fee: str) -> Case:
    case = _make_case_with_quote(amount, "goods_and_services", "standard")
    adapter = QuoteAdapter()
    manual_flow._run_reconciliation(case, _details(amount=amount, fee=fee), adapter)
    assert case.status == CaseStatus.RECONCILED
    return case


def test_audit_fields_in_reconciled_result() -> None:
    """Every reconciled result must contain the required audit fields."""
    case = _make_case_with_quote("1.00", "goods_and_services", "standard")
    adapter = QuoteAdapter()
    returned = manual_flow._run_reconciliation(case, _details(), adapter)
    assert returned.status == CaseStatus.RECONCILED
    assert returned.product_selection_source == "explicit_execution_path_mapping"
    assert returned.prediction_provenance == "pre_submission_prediction"
    assert returned.prediction_created_before_original_submission is True
    assert returned.prediction_created_before_observation_reuse is True
    assert returned.original_submission_timestamp_known is True
    assert returned.prediction_sha256 is not None
    assert returned.prediction_unchanged_after_observation is True
    assert returned.quote["_schedule_metadata"]["base_rule_id"]


def test_manual_plan_fails_when_mapping_not_calculable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mapping that is not calculable for the merchant market must fail at plan time."""
    monkeypatch.setattr("paypal_sandbox_validation.accounts.EXPECTED_COUNTRIES", {"DE"})

    def raise_unavailable(*args, **kwargs):
        from paypal_sandbox_validation.quote_adapter import QuoteResolutionError

        raise QuoteResolutionError("not calculable", status="account_capability_unavailable")

    monkeypatch.setattr(QuoteAdapter, "resolve_manual_scenario", raise_unavailable)

    accounts_csv = _de_account_csv(tmp_path)
    plan = manual_flow.build_manual_plan("run-2", "manual-de-formula", str(accounts_csv), [("DE", "DE", "1.00", "EUR")])
    assert len(plan) == 1
    case = plan[0]
    assert case.status == CaseStatus.FAILED
    assert case.paypal_issue == ReconciliationStatus.ACCOUNT_CAPABILITY_UNAVAILABLE.value
