"""Tests for PayPal Sandbox merchant qualification and regional validation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from paypal_sandbox_validation.models import Case, CaseStatus, QualificationStatus
from paypal_sandbox_validation.qualification import (
    _library_observations_for_buyers,
    build_qualification_plan,
    build_validation_plan,
    classify_manual_send_pricing,
    classify_qualification,
    is_merchant_excluded,
    load_qualification_registry,
    promote_observation_fixtures,
    save_qualification_registry,
    save_qualification_report,
    update_manual_send_qualification,
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
    execution_classification: str = "public_rate_validation",
    planning_time_registry_status: str | None = "representative",
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
        execution_classification=execution_classification,
        planning_time_registry_status=planning_time_registry_status,
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


def test_classify_jpy_zero_decimal_representative() -> None:
    """JPY zero-decimal observations are compared using integer minor units."""
    jp = _mock_case("JP", "JP", "1000", "JPY", "32", "32")
    jp_us = _mock_case("JP", "US", "1000", "JPY", "32", "32")

    class _JPYAdapter:
        def build_quote(
            self, merchant_country: str, buyer_country: str, amount: str, currency: str, **_: Any
        ) -> dict[str, Any]:
            return {
                "status": "exact_for_public_rate",
                "amount": {"value": amount, "currency": currency},
                "processing_fee": {"value": "32", "currency": currency},
                "gross_amount": {"value": amount, "currency": currency},
                "net_amount": {"value": str(1000 - 32), "currency": currency},
                "components": [{"type": "processing", "amount": "32", "currency": currency}],
                "_schedule_metadata": {"base_percentage": "3.0", "fixed_amount": "0"},
                "data": {"content_sha256": "sha", "data_ref": "local"},
            }

    result = classify_qualification("JP", [jp, jp_us], _JPYAdapter())
    assert result["status"] == QualificationStatus.REPRESENTATIVE


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
    excluded = _mock_case(
        "GB",
        "AU",
        "10.00",
        "GBP",
        "0.59",
        "0.79",
        execution_classification="diagnostic_sandbox_pricing",
        planning_time_registry_status="sandbox_specific_pricing",
    )
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
    assert summary["live_captures_completed"] == 3
    assert summary["cases_reconciled"] == 3
    assert summary["domestic_matches"] == 1
    assert summary["surcharge_matches"] == 1
    assert summary["fee_mismatches"] == 1


def test_validation_summary_separates_live_and_reused_counters() -> None:
    live = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    reused = _mock_case("US", "AU", "10.00", "USD", "0.99", "0.99")
    reused.pilot_metadata["reused_observation"] = True
    reused.reconciliation = {"status": "historical_observation_current_mismatch"}
    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}
    summary = validation_summary([live, reused], registry)
    assert summary["cases_reconciled"] == 2
    assert summary["live_captures_completed"] == 1
    assert summary["historical_observations_reused"] == 1
    assert summary["historical_observation_current_mismatches"] == 1


def test_library_observations_match_public_schedule() -> None:
    adapter = QuoteAdapter()
    obs = _library_observations_for_buyers("US", {"US", "AU"}, "10.00", "USD", adapter)
    assert len(obs) == 2
    assert any(o["buyer_country"] == "US" for o in obs)
    assert any(o["buyer_country"] == "AU" for o in obs)


def _mock_manual_send_case(
    amount: str,
    paypal_fee: str,
    library_fee: str,
    provenance: str = "pre_submission_prediction",
) -> Case:
    currency = "EUR"
    quote = {
        "status": "exact_for_public_rate",
        "processing_fee": {"value": library_fee, "currency": currency},
        "gross_amount": {"value": amount, "currency": currency},
        "net_amount": {"value": str(Decimal(amount) - Decimal(paypal_fee)), "currency": currency},
        "components": [{"type": "processing", "amount": library_fee, "currency": currency}],
        "_schedule_metadata": {
            "base_percentage": "2.49",
            "fixed_amount": "0.35",
            "payer_region": "DE",
            "fixed_fee_schedule_id": "goods_and_services",
            "international_surcharge_schedule_id": "goods_and_services",
            "base_rule_id": "paypal:DE:goods_and_services:standard:base",
        },
        "matched_rules": [{"rule_id": "paypal:DE:goods_and_services:standard:base"}],
        "data": {"content_sha256": "sha", "data_ref": "local"},
    }
    evidence = {
        "status": "COMPLETED",
        "gross_amount": {"value": amount, "currency_code": currency},
        "paypal_fee": {"value": paypal_fee, "currency_code": currency},
        "net_amount": {"value": str(Decimal(amount) - Decimal(paypal_fee)), "currency_code": currency},
        "payer_country": "DE",
    }
    return Case(
        case_id=f"test-DE-DE-{amount}",
        run_id="r1",
        merchant_country="DE",
        buyer_country="DE",
        amount=amount,
        currency=currency,
        execution_path="manual_send_to_business",
        product_id="goods_and_services",
        variant_id="standard",
        status=CaseStatus.RECONCILED,
        quote=quote,
        paypal_evidence=evidence,
        reconciliation={"status": "match" if paypal_fee == library_fee else "fee_mismatch"},
        prediction_provenance=provenance,
        prediction_created_before_original_submission=(provenance == "pre_submission_prediction"),
        prediction_created_before_observation_reuse=True,
        original_submission_timestamp_known=True,
    )


def test_classify_manual_send_pricing_uses_only_fresh_observations() -> None:
    """The observed account formula is inferred from EUR 10 and EUR 100 only."""
    fresh_cases = [
        _mock_manual_send_case("10.00", "0.54", "0.60"),
        _mock_manual_send_case("100.00", "2.25", "2.84"),
    ]
    historical = _mock_manual_send_case("1.00", "0.37", "0.37", provenance="historical_observation_requoted")
    observation = classify_manual_send_pricing(fresh_cases + [historical])
    assert observation["status"] == "sandbox_specific_pricing"
    assert observation["public_formula"]["percentage"] == "2.49"
    assert observation["public_formula"]["fixed"]["value"] == "0.35"
    assert observation["observed_account_formula"]["percentage"] == "1.90"
    assert observation["observed_account_formula"]["fixed"]["value"] == "0.35"
    assert observation["classification"] == "sandbox_account_pricing_difference"
    assert observation["usable_for_public_rate_validation"] is False


def test_classify_manual_send_pricing_representative() -> None:
    """When every fresh observation matches the public formula the merchant is representative."""
    cases = [
        _mock_manual_send_case("1.00", "0.37", "0.37"),
        _mock_manual_send_case("10.00", "0.60", "0.60"),
        _mock_manual_send_case("100.00", "2.84", "2.84"),
    ]
    classification = classify_manual_send_pricing(cases)
    assert classification["status"] == "representative"
    assert classification["classification"] == "representative"
    assert classification["usable_for_public_rate_validation"] is True


def test_classify_manual_send_pricing_inconclusive_when_unstable() -> None:
    """A non-linear set of observations cannot be reported as sandbox-specific."""
    cases = [
        _mock_manual_send_case("1.00", "0.37", "0.37"),
        _mock_manual_send_case("10.00", "0.54", "0.60"),
        _mock_manual_send_case("100.00", "2.00", "2.84"),
    ]
    classification = classify_manual_send_pricing(cases)
    assert classification["status"] == "inconclusive"
    assert classification["classification"] == "inconclusive"
    assert classification["usable_for_public_rate_validation"] is False


def test_update_manual_send_qualification_marks_de_not_representative() -> None:
    """DE manual-send pricing classification excludes the merchant from public-rate validation."""
    observation = {
        "merchant_country": "DE",
        "execution_path": "manual_send_to_business",
        "product_id": "goods_and_services",
        "variant_id": "standard",
        "status": "sandbox_specific_pricing",
        "public_formula": {"percentage": "2.49", "fixed": {"value": "0.35", "currency": "EUR"}},
        "observed_account_formula": {"percentage": "1.90", "fixed": {"value": "0.35", "currency": "EUR"}},
        "classification": "sandbox_account_pricing_difference",
        "confidence": "high",
        "usable_for_public_rate_validation": False,
    }
    registry: dict[str, Any] = {"DE": {"orders_v2_checkout": "sandbox_checkout_limitation"}}
    update_manual_send_qualification(registry, "DE", observation)
    assert registry["DE"]["manual_send_to_business"] == "sandbox_specific_pricing"
    assert registry["DE"]["orders_v2_checkout"] == "sandbox_checkout_limitation"
    assert registry["DE"]["representative_for_public_rates"] is False
    assert is_merchant_excluded("DE", registry) is True


def test_validation_summary_excludes_diagnostic_merchants() -> None:
    """Diagnostic/sandbox-pricing matches do not count as public-rate matches."""
    us = _mock_case("US", "US", "10.00", "USD", "0.84", "0.84")
    de_diagnostic = _mock_case(
        "DE",
        "US",
        "10.00",
        "EUR",
        "0.54",
        "0.54",
        execution_classification="diagnostic_sandbox_pricing",
        planning_time_registry_status="sandbox_specific_pricing",
    )
    de_diagnostic.reconciliation = {"status": "match"}
    registry = {
        "US": {"status": QualificationStatus.REPRESENTATIVE},
        "DE": {
            "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING,
            "representative_for_public_rates": False,
        },
    }
    summary = validation_summary([us, de_diagnostic], registry)
    assert summary["diagnostic_merchants"] == 1
    assert summary["representative_captures_completed"] == 1
    assert summary["diagnostic_captures_completed"] == 1
    assert summary["domestic_matches"] == 1
    assert summary["surcharge_matches"] == 0


def test_save_qualification_registry_finalizes_au_surcharge_status(tmp_path: Path) -> None:
    """The AU entry is normalized to confirm the surcharge only for the tested case."""
    registry = {
        "AU": {
            "merchant_country": "AU",
            "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING.value,
            "classification": QualificationStatus.SANDBOX_SPECIFIC_PRICING.value,
            "observed_account_formula": {"percentage": "2.40", "fixed": {"value": "0.30", "currency": "AUD"}},
            "public_formula": {"percentage": "2.9", "fixed": {"value": "0.30", "currency": "AUD"}},
            "representative_for_public_rates": False,
        }
    }
    path = tmp_path / "registry.json"
    save_qualification_registry(registry, path=path)
    updated = json.loads(path.read_text())["AU"]
    assert updated["international_surcharge_status"] == "confirmed_for_tested_case"
    assert updated["international_surcharge_percentage_points"] == "1.00"
    assert updated["confirmed_case"] == "AU merchant ← US buyer, AUD 10.00"
    assert updated["cross_region_generalization"] == "not_yet_tested"
    assert "not yet tested" in updated["reason"].lower()
    assert updated["representative_for_public_rates"] is False
