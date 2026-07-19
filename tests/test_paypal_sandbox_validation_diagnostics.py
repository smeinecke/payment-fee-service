"""Tests for PayPal Sandbox fee-mismatch diagnostics."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from paypal_sandbox_validation.diagnostics import (
    build_observations_from_run,
    classify_root_cause,
    decompose_case,
    infer_formula,
    load_original_case,
    validate_case_constraints,
)
from paypal_sandbox_validation.models import Case, CaseStatus


def _mock_quote(amount: str, currency: str = "GBP") -> dict:
    return {
        "gross_amount": {"value": amount, "currency": currency},
        "processing_fee": {"value": "0.79", "currency": currency},
        "net_amount": {"value": "9.21", "currency": currency},
        "components": [
            {
                "type": "processing",
                "amount": "0.59",
                "currency": currency,
                "rate_percentage": "2.9",
                "fixed_amount": "0.30",
                "source_rule_id": "paypal:GB:other_commercial:standard:base",
            },
            {
                "type": "surcharge",
                "amount": "0.20",
                "currency": currency,
                "rate_percentage": "1.99",
                "fixed_amount": None,
                "source_rule_id": "paypal:GB:other_commercial:standard:surcharge:OTHER",
            },
        ],
        "_schedule_metadata": {
            "base_percentage": "2.9",
            "base_rule_id": "paypal:GB:other_commercial:standard:base",
            "component_signature": [("processing", "0.59"), ("surcharge", "0.20")],
            "fixed_amount": "0.30",
            "fixed_fee_schedule_id": "other_commercial",
            "international_surcharge_schedule_id": "other_commercial",
            "payer_region": "OTHER",
            "predicted_total_fee": "0.79",
            "surcharge_percentage": "1.99",
        },
        "_request": {
            "account_country": "GB",
            "amount": {"value": amount, "currency": currency},
            "customer_country": "AU",
            "provider": "paypal",
            "settlement_currency": currency,
            "transaction": {
                "channel": "online",
                "funding_source": "paypal_balance",
                "payer_region": "OTHER",
                "payment_method": "paypal_wallet",
                "product_id": "other_commercial",
                "transaction_region": "domestic",
                "variant_id": "standard",
            },
        },
        "_scenario": {"product_id": "other_commercial", "variant_id": "standard"},
    }


def _make_case(tmp_path: Path, case_id: str = "regional-GB-AU-2") -> Case:
    return Case(
        case_id=case_id,
        run_id="r1",
        merchant_country="GB",
        buyer_country="AU",
        amount="10.00",
        currency="GBP",
        product_id="other_commercial",
        variant_id="standard",
        status=CaseStatus.RECONCILED,
        paypal_evidence={
            "gross_amount": {"currency_code": "GBP", "value": "10.00"},
            "paypal_fee": {"currency_code": "GBP", "value": "0.59"},
            "net_amount": {"currency_code": "GBP", "value": "9.41"},
            "payer_country": "AU",
            "status": "COMPLETED",
        },
        quote=_mock_quote("10.00"),
    )


def _patch_run_dirs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "paypal_sandbox_validation.diagnostics.run_dir",
        lambda rid, _base=None: tmp_path / rid,
    )
    monkeypatch.setattr(
        "paypal_sandbox_validation.persistence.run_dir",
        lambda rid, _base=None: tmp_path / rid,
    )


@pytest.fixture
def run_fixture(tmp_path: Path, monkeypatch) -> str:
    run_id = "test-run-1234"
    _patch_run_dirs(monkeypatch, tmp_path)
    (tmp_path / run_id).mkdir(parents=True, exist_ok=True)
    case = _make_case(tmp_path).model_dump()
    (tmp_path / run_id / "cases").mkdir(exist_ok=True)
    (tmp_path / run_id / "cases" / "regional-GB-AU-2.json").write_text(json.dumps(case))
    return run_id


def test_load_original_case(run_fixture: str, tmp_path: Path, monkeypatch) -> None:
    _patch_run_dirs(monkeypatch, tmp_path)
    case = load_original_case(run_fixture, "regional-GB-AU-2")
    assert case.merchant_country == "GB"
    assert case.buyer_country == "AU"
    assert case.quote["processing_fee"]["value"] == "0.79"


def test_validate_case_constraints_passes() -> None:
    case = _make_case(Path("/tmp"))
    result = validate_case_constraints(case)
    assert result["valid"] is True
    assert result["classification"] is None


def test_validate_buyer_country_mismatch() -> None:
    case = _make_case(Path("/tmp"))
    case.paypal_evidence["payer_country"] = "US"
    result = validate_case_constraints(case)
    assert result["valid"] is False
    assert result["classification"] == "buyer_country_mismatch"


def test_validate_fx_exclusion() -> None:
    case = _make_case(Path("/tmp"))
    case.paypal_evidence["paypal_fee"]["currency_code"] = "USD"
    case.paypal_evidence["net_amount"]["currency_code"] = "USD"
    result = validate_case_constraints(case)
    assert result["valid"] is False
    assert result["classification"] == "excluded_fx_case"


def test_validate_paypal_gross_fee_net_invariant() -> None:
    case = _make_case(Path("/tmp"))
    case.paypal_evidence["net_amount"]["value"] = "9.00"
    result = validate_case_constraints(case)
    assert result["valid"] is False
    assert result["classification"] == "paypal_fee_data_defect"


def test_validate_library_component_sum() -> None:
    case = _make_case(Path("/tmp"))
    case.quote["processing_fee"]["value"] = "1.00"
    result = validate_case_constraints(case)
    assert result["valid"] is False
    assert result["classification"] == "payment_fee_calculation_or_rounding_defect"


def test_decompose_financial_uses_decimal() -> None:
    case = _make_case(Path("/tmp"))
    decomposition = decompose_case(case)
    assert decomposition["paypal"]["gross"] == "10.00"
    assert decomposition["library_base"]["calculated_percentage_amount"] == "0.29"
    assert decomposition["library_surcharge"]["calculated_surcharge_amount"] == "0.20"
    assert decomposition["library"]["final_fee"] == "0.79"
    assert decomposition["library"]["rounding_point"] == "after component aggregation"


def test_infer_percentage_plus_fixed_three_amounts() -> None:
    observations = [
        {"amount": "1.00", "paypal_fee": "0.24", "buyer_country": "AU", "observed_payer_country": "AU"},
        {"amount": "10.00", "paypal_fee": "0.59", "buyer_country": "AU", "observed_payer_country": "AU"},
        {"amount": "100.00", "paypal_fee": "4.10", "buyer_country": "AU", "observed_payer_country": "AU"},
    ]
    formula = infer_formula(observations)
    assert formula["stable_linear_formula_found"] is True
    best = formula["best"]
    assert best["formula_type"] == "percentage_plus_fixed"
    assert Decimal(best["percentage"]) == Decimal("3.9")
    assert Decimal(best["fixed"]) == Decimal("0.20")
    assert all(e == 0 for e in best["errors_minor"])


def test_infer_base_plus_surcharge_fits_library() -> None:
    observations = [
        {"amount": "10.00", "paypal_fee": "0.79", "buyer_country": "AU", "observed_payer_country": "AU"},
    ]
    formula = infer_formula(observations, base_pct=Decimal("2.9"), surcharge_pct=Decimal("1.99"), fixed=Decimal("0.30"))
    best = formula["best"]
    assert best["formula_type"] == "base_plus_surcharge_plus_fixed"
    assert all(e == 0 for e in best["errors_minor"])


def test_classify_sandbox_account_configuration() -> None:
    case = _make_case(Path("/tmp"))
    decomposition = decompose_case(case)
    observations = [
        {"amount": "1.00", "paypal_fee": "0.24", "buyer_country": "AU"},
        {"amount": "10.00", "paypal_fee": "0.59", "buyer_country": "AU"},
        {"amount": "100.00", "paypal_fee": "4.10", "buyer_country": "AU"},
    ]
    formula = infer_formula(observations)
    root_cause = classify_root_cause(case, decomposition, formula)
    assert root_cause["category"] == "sandbox_account_configuration"
    assert root_cause["confidence"] == "high"


def test_classify_buyer_country_mismatch_skips_analysis() -> None:
    case = _make_case(Path("/tmp"))
    case.paypal_evidence["payer_country"] = "US"
    # Validation should classify before any fee-model analysis is attempted.
    assert validate_case_constraints(case)["classification"] == "buyer_country_mismatch"


def test_diagnostic_output_is_secret_free(run_fixture: str, tmp_path: Path, monkeypatch) -> None:
    from paypal_sandbox_validation.diagnostics import generate_diagnostic_reports

    _patch_run_dirs(monkeypatch, tmp_path)
    case = load_original_case(run_fixture, "regional-GB-AU-2")
    decomposition = decompose_case(case)
    observations = [
        {
            "amount": "10.00",
            "currency": "GBP",
            "paypal_fee": "0.59",
            "buyer_country": "AU",
            "observed_payer_country": "AU",
        },
    ]
    formula = infer_formula(observations)
    root_cause = classify_root_cause(case, decomposition, formula)
    reports = generate_diagnostic_reports(
        run_fixture, "regional-GB-AU-2", case, decomposition, formula, root_cause, observations, None
    )
    diagnostic = json.loads(reports["diagnostic_json"].read_text())
    text = json.dumps(diagnostic)
    assert "payer_id" not in text
    assert "capture_id" not in text
    assert "order_id" not in text
    assert "request_id" not in text
    assert "approval_url" not in text
    assert diagnostic["observed_payer_country"] == "AU"


def test_build_observations_from_run(tmp_path: Path, monkeypatch) -> None:
    run_id = "obs-run"
    _patch_run_dirs(monkeypatch, tmp_path)
    (tmp_path / run_id).mkdir(parents=True, exist_ok=True)
    results = {
        "run_id": run_id,
        "cases": [
            {
                "case_id": "c1",
                "merchant_country": "GB",
                "buyer_country": "AU",
                "currency": "GBP",
                "paypal_evidence": {
                    "status": "COMPLETED",
                    "gross_amount": {"value": "10.00", "currency_code": "GBP"},
                    "paypal_fee": {"value": "0.59", "currency_code": "GBP"},
                    "payer_country": "AU",
                },
            },
            {
                "case_id": "c2",
                "merchant_country": "GB",
                "buyer_country": "GB",
                "currency": "GBP",
                "paypal_evidence": None,
            },
        ],
    }
    (tmp_path / run_id / "results.json").write_text(json.dumps(results))
    observations = build_observations_from_run(run_id, "GB", currency="GBP")
    assert len(observations) == 1
    assert observations[0]["amount"] == "10.00"
    assert observations[0]["paypal_fee"] == "0.59"


def test_three_amount_formula_inference_against_library_rounding() -> None:
    """Confirm per-component rounding and aggregate rounding match for standard amounts."""
    observations = [
        {"amount": "1.00", "paypal_fee": "0.35", "buyer_country": "AU"},
        {"amount": "10.00", "paypal_fee": "0.79", "buyer_country": "AU"},
        {"amount": "100.00", "paypal_fee": "5.19", "buyer_country": "AU"},
    ]
    formula = infer_formula(observations, base_pct=Decimal("2.9"), surcharge_pct=Decimal("1.99"), fixed=Decimal("0.30"))
    # Both a single percentage-plus-fixed and base+surcharge can fit simple
    # round numbers; verify the base-plus-surcharge candidate is discovered and fits.
    base_surcharge_candidates = [
        c for c in formula["candidates"] if c["formula_type"] == "base_plus_surcharge_plus_fixed"
    ]
    assert base_surcharge_candidates
    assert base_surcharge_candidates[0]["fit"] is True
