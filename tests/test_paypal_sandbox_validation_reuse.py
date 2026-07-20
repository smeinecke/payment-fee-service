from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from paypal_sandbox_validation.cli import _attempt_public_rate_reuse
from paypal_sandbox_validation.models import Case, CaseStatus, QualificationStatus, ReconciliationStatus
from paypal_sandbox_validation.qualification import promote_observation_fixtures, validation_summary


def _public_case(merchant: str, buyer: str, amount: str, currency: str) -> Case:
    return Case(
        case_id=f"{merchant}-{buyer}-{amount}",
        run_id="r1",
        merchant_country=merchant,
        buyer_country=buyer,
        amount=amount,
        currency=currency,
        product_id="paypal_checkout",
        variant_id="standard",
        status=CaseStatus.PLANNED,
        execution_classification="public_rate_validation",
        planning_time_registry_status=QualificationStatus.REPRESENTATIVE.value,
    )


def _fixture(
    merchant: str,
    buyer: str,
    amount: str,
    currency: str,
    fee: str,
    data_revision: str = "old-sha",
    rule_ids: list[str] | None = None,
    schedule_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "result": "match",
        "merchant_country": merchant,
        "buyer_country": buyer,
        "amount": {"value": amount, "currency": currency},
        "paypal_fee": {"value": fee, "currency": currency},
        "observed_payer_country": buyer,
        "product_id": "paypal_checkout",
        "variant_id": "standard",
        "data_revision": data_revision,
        "rule_ids": rule_ids or ["rule-old"],
        "schedule_ids": schedule_ids or ["sched-old"],
    }


def _adapter_quote(
    fee: str,
    currency: str,
    data_revision: str = "new-sha",
    base_pct: str = "2.9",
    surcharge_pct: str | None = None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = [
        {"type": "processing", "amount": fee, "currency": currency},
    ]
    return {
        "status": "exact_for_public_rate",
        "amount": {"value": "10.00", "currency": currency},
        "processing_fee": {"value": fee, "currency": currency},
        "gross_amount": {"value": "10.00", "currency": currency},
        "net_amount": {"value": str(Decimal("10.00") - Decimal(fee)), "currency": currency},
        "components": components,
        "_schedule_metadata": {
            "base_percentage": base_pct,
            "fixed_amount": "0.30",
            "fixed_fee_schedule_id": "paypal_checkout",
            "international_surcharge_schedule_id": "paypal_checkout",
        },
        "matched_rules": [{"rule_id": "rule-new"}],
        "data": {"content_sha256": data_revision, "data_ref": "local"},
    }


def test_public_rate_reuse_requotes_with_current_revision(tmp_path: Path) -> None:
    """Reused historical evidence is reconciled against a current quote."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    fixture_path = fixtures_dir / "us-us.json"
    fixture_path.write_text(json.dumps(_fixture("US", "US", "10.00", "USD", "0.84")))

    case = _public_case("US", "US", "10.00", "USD")
    plan = [case]
    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}

    class _Adapter:
        def build_quote(
            self, merchant_country: str, buyer_country: str, amount: str, currency: str, **_: Any
        ) -> dict[str, Any]:
            return _adapter_quote("0.84", currency)

    _attempt_public_rate_reuse(plan, registry, _Adapter(), fixtures_dir=fixtures_dir)

    assert case.status == CaseStatus.RECONCILED
    assert case.reconciliation.get("status") == "match"
    assert case.quote["data"]["content_sha256"] == "new-sha"
    assert case.pilot_metadata["source_data_revision"] == "old-sha"
    assert case.pilot_metadata["current_data_revision"] == "new-sha"
    assert case.pilot_metadata["historical_fixture_result"] == "match"


def test_public_rate_reuse_records_current_mismatch(tmp_path: Path) -> None:
    """A historical fixture that no longer matches the current quote is not treated as a match."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    fixture_path = fixtures_dir / "us-us.json"
    fixture_path.write_text(json.dumps(_fixture("US", "US", "10.00", "USD", "0.84")))

    case = _public_case("US", "US", "10.00", "USD")
    plan = [case]
    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}

    class _Adapter:
        def build_quote(
            self, merchant_country: str, buyer_country: str, amount: str, currency: str, **_: Any
        ) -> dict[str, Any]:
            return _adapter_quote("0.99", currency)

    _attempt_public_rate_reuse(plan, registry, _Adapter(), fixtures_dir=fixtures_dir)

    assert case.status == CaseStatus.RECONCILED
    assert case.reconciliation.get("status") == ReconciliationStatus.HISTORICAL_OBSERVATION_CURRENT_MISMATCH.value
    assert case.pilot_metadata["reused_observation"] is True
    assert case.pilot_metadata["current_delta_minor_units"] == 15


def test_reused_fixture_is_not_promoted_as_new_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reused observations cannot be written out as new positive fixtures."""
    fixtures_dir = tmp_path / "fixtures"
    diagnostics_dir = tmp_path / "diagnostics"

    reused_match = _public_case("US", "US", "10.00", "USD")
    reused_match.status = CaseStatus.RECONCILED
    reused_match.reconciliation = {"status": "match"}
    reused_match.paypal_evidence = {
        "status": "COMPLETED",
        "gross_amount": {"value": "10.00", "currency_code": "USD"},
        "paypal_fee": {"value": "0.84", "currency_code": "USD"},
        "net_amount": {"value": "9.16", "currency_code": "USD"},
        "payer_country": "US",
    }
    reused_match.pilot_metadata["reused_observation"] = True

    paths = promote_observation_fixtures(
        [reused_match],
        fixtures_dir=fixtures_dir,
        diagnostics_dir=diagnostics_dir,
    )
    assert paths == []


def test_validation_summary_does_not_count_reused_mismatch_as_new_fixture(tmp_path: Path) -> None:
    """New-positive-fixture counter only counts freshly generated positive fixtures."""
    live_match = _public_case("US", "US", "10.00", "USD")
    live_match.status = CaseStatus.RECONCILED
    live_match.reconciliation = {"status": "match"}
    live_match.quote = _adapter_quote("0.84", "USD")
    live_match.paypal_evidence = {
        "status": "COMPLETED",
        "gross_amount": {"value": "10.00", "currency_code": "USD"},
        "paypal_fee": {"value": "0.84", "currency_code": "USD"},
        "net_amount": {"value": "9.16", "currency_code": "USD"},
        "payer_country": "US",
    }

    fixtures_dir = tmp_path / "fixtures"
    diagnostics_dir = tmp_path / "diagnostics"
    fixture_paths = promote_observation_fixtures(
        [live_match],
        fixtures_dir=fixtures_dir,
        diagnostics_dir=diagnostics_dir,
    )

    registry = {"US": {"status": QualificationStatus.REPRESENTATIVE}}
    summary = validation_summary([live_match], registry, fixture_paths=fixture_paths)
    assert summary["new_positive_fixtures_generated"] == 1
    assert summary["live_captures_completed"] == 1
