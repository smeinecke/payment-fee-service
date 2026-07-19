"""Tests for PayPal Sandbox merchant qualification and regional validation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from paypal_sandbox_validation.models import Case, CaseStatus, QualificationStatus
from paypal_sandbox_validation.qualification import (
    _library_observations_for_buyers,
    build_qualification_plan,
    build_validation_plan,
    classify_qualification,
    is_merchant_excluded,
    load_qualification_registry,
    promote_observation_fixtures,
    save_qualification_registry,
    save_qualification_report,
    validation_summary,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter


def _mock_case(
    merchant: str,
    buyer: str,
    amount: str,
    currency: str,
    paypal_fee: str,
    library_fee: str,
    status: CaseStatus = CaseStatus.RECONCILED,
    reconciliation_status: str = "match",
    paypal_issue: str | None = None,
    payer_country: str | None = None,
) -> Case:
    if payer_country is None:
        payer_country = buyer
    quote = {
        "status": "exact_for_public_rate",
        "processing_fee": {"value": library_fee, "currency": currency},
        "gross_amount": {"value": amount, "currency": currency},
        "net_amount": {"value": str(Decimal(amount) - Decimal(paypal_fee)), "currency": currency},
        "components": [{"type": "processing", "amount": library_fee, "currency": currency}],
        "_schedule_metadata": {
            "base_percentage": "3.0",
            "fixed_amount": "0.30",
            "payer_region": "US",
            "fixed_fee_schedule_id": "paypal_checkout",
            "international_surcharge_schedule_id": "paypal_checkout",
        },
        "matched_rules": [{"rule_id": f"paypal:{merchant}:paypal_checkout:standard:base"}],
        "data": {"content_sha256": "sha", "data_ref": "local"},
    }
    evidence = {
        "status": "COMPLETED",
        "gross_amount": {"value": amount, "currency_code": currency},
        "paypal_fee": {"value": paypal_fee, "currency_code": currency},
        "net_amount": {"value": str(Decimal(amount) - Decimal(paypal_fee)), "currency_code": currency},
        "payer_country": payer_country,
    }
    return Case(
        case_id=f"test-{merchant}-{buyer}",
        run_id="r1",
        merchant_country=merchant,
        buyer_country=buyer,
        amount=amount,
        currency=currency,
        product_id="paypal_checkout",
        variant_id="standard",
        status=status,
        quote=quote,
        paypal_evidence=evidence,
        reconciliation={"status": reconciliation_status},
        paypal_issue=paypal_issue,
    )


def test_qualification_registry_excludes_unsuitable(tmp_path: Path) -> None:
    registry = {
        "GB": {"status": QualificationStatus.SANDBOX_SPECIFIC_PRICING, "reason": "test"},
        "DE": {"status": QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED, "reason": "test"},
        "US": {"status": QualificationStatus.REPRESENTATIVE, "reason": "test"},
    }
    assert is_merchant_excluded("GB", registry) is True
    assert is_merchant_excluded("DE", registry) is True
    assert is_merchant_excluded("US", registry) is False
    assert is_merchant_excluded("XX", registry) is False
    assert is_merchant_excluded("GB", registry, override=True) is False


def test_qualification_registry_persisted(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    registry = {"JP": {"status": QualificationStatus.DATASET_NOT_CALCULABLE, "reason": "no calculable rule"}}
    save_qualification_registry(registry, path)
    loaded = load_qualification_registry(path)
    assert loaded == registry


def test_invalid_evidence_classifications() -> None:
    valid = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    from paypal_sandbox_validation.diagnostics import validate_case_constraints

    assert validate_case_constraints(valid)["valid"] is True

    invalid_gross_fee_net = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    invalid_gross_fee_net.paypal_evidence["net_amount"]["value"] = "9.00"
    result = validate_case_constraints(invalid_gross_fee_net)
    assert result["valid"] is False
    assert result["classification"] == "paypal_api_evidence_invalid"

    missing_evidence = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    missing_evidence.paypal_evidence = {"status": "COMPLETED", "payer_country": "US"}
    missing_evidence.quote = None
    result = validate_case_constraints(missing_evidence)
    assert result["valid"] is False
    assert result["classification"] == "harness_evidence_defect"


def test_classify_representative_merchant() -> None:
    adapter = QuoteAdapter()
    us = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    us_foreign = _mock_case("US", "AU", "10.00", "USD", "0.99", "0.99")
    result = classify_qualification("US", [us, us_foreign], adapter)
    assert result["status"] == QualificationStatus.REPRESENTATIVE


def test_classify_sandbox_specific_pricing() -> None:
    adapter = QuoteAdapter()
    us = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    # PayPal applies the same domestic fee to a foreign buyer even though the
    # library predicts a different schedule fee.
    us_foreign = _mock_case("US", "AU", "10.00", "USD", "0.84", "0.99")
    result = classify_qualification("US", [us, us_foreign], adapter)
    assert result["status"] == QualificationStatus.SANDBOX_SPECIFIC_PRICING


def test_classify_inconclusive_with_one_observation() -> None:
    adapter = QuoteAdapter()
    only = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    result = classify_qualification("US", [only], adapter)
    assert result["status"] == QualificationStatus.INCONCLUSIVE


def test_build_qualification_plan_limits_three_cases() -> None:
    adapter = QuoteAdapter()
    buyer_countries = {"US", "CA", "AU", "GB", "DE"}
    plan = build_qualification_plan("r1", ["US"], buyer_countries, adapter, max_cases_per_merchant=3)
    assert len([c for c in plan if c.merchant_country == "US"]) <= 3
    assert any(c.buyer_country == "US" for c in plan if c.merchant_country == "US")


def test_build_validation_plan_one_domestic_one_foreign() -> None:
    adapter = QuoteAdapter()
    buyer_countries = {"US", "CA", "AU", "GB", "DE"}
    plan = build_validation_plan("r1", ["US"], buyer_countries, adapter)
    us_cases = [c for c in plan if c.merchant_country == "US"]
    assert len(us_cases) == 2
    assert any(c.buyer_country == "US" for c in us_cases)
    assert any(c.buyer_country != "US" for c in us_cases)


def test_observation_fixture_eligibility(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "paypal_sandbox_validation.qualification.load_qualification_registry",
        lambda _path=None: {"US": {"status": QualificationStatus.REPRESENTATIVE}},
    )
    representative_match = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    excluded = _mock_case("GB", "AU", "10.00", "GBP", "0.59", "0.79")
    mismatch = _mock_case("US", "AU", "10.00", "USD", "0.84", "0.99")
    mismatch.reconciliation = {"status": "fee_mismatch"}

    fixtures_dir = tmp_path / "fixtures"
    diagnostics_dir = tmp_path / "diagnostics"
    paths = promote_observation_fixtures(
        [representative_match, excluded, mismatch], fixtures_dir=fixtures_dir, diagnostics_dir=diagnostics_dir
    )
    assert len(paths) == 3
    positive = [p for p in paths if p.parent == fixtures_dir and p.name.startswith("r1-test-US")]
    assert len(positive) == 1
    positive_data = json.loads(positive[0].read_text())
    assert positive_data["result"] == "match"
    assert "order_id" not in positive_data
    assert "capture_id" not in positive_data


def test_secret_free_qualification_report(tmp_path: Path) -> None:
    adapter = QuoteAdapter()
    us = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    us_au = _mock_case("US", "AU", "10.00", "USD", "0.99", "0.99")
    qualification = classify_qualification("US", [us, us_au], adapter)
    registry = {"US": qualification}
    paths = save_qualification_report("r2", registry, [us, us_au], output_dir=tmp_path / "r2")
    report = json.loads(paths["json"].read_text())
    text = json.dumps(report)
    assert "payer_id" not in text
    assert "capture_id" not in text
    assert "approval_url" not in text
    assert report["summary"]["merchants_representative"] == 1


def test_validation_summary_counts() -> None:
    us = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    us_au = _mock_case("US", "AU", "10.00", "USD", "0.99", "0.99")
    mismatch = _mock_case("CA", "AU", "10.00", "CAD", "0.79", "0.99")
    mismatch.reconciliation = {"status": "fee_mismatch"}
    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}
    summary = validation_summary([us, us_au, mismatch], registry)
    assert summary["captures_completed"] == 3
    assert summary["domestic_matches"] == 1
    assert summary["surcharge_matches"] == 1
    assert summary["fee_mismatches"] == 1


def test_library_observations_match_public_schedule() -> None:
    adapter = QuoteAdapter()
    obs = _library_observations_for_buyers("US", {"US", "AU"}, "10.00", "USD", adapter)
    assert len(obs) == 2
    assert any(o["buyer_country"] == "US" for o in obs)
    assert any(o["buyer_country"] == "AU" for o in obs)
