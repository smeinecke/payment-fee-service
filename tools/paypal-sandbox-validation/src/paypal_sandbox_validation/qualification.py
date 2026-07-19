"""Merchant qualification logic for the PayPal Sandbox regional pilot.

Qualification runs a bounded, three-capture calibration per Merchant to decide
whether that Merchant's Sandbox account behaves like the published public fee
schedule before it is allowed into the normal validation matrix.
"""

from __future__ import annotations

import contextlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .configuration import currency_for_country
from .diagnostics import (
    _decimal,
    infer_formula,
    validate_case_constraints,
)
from .models import Case, CaseStatus, QualificationStatus
from .planner import _case_from_quote, _quote_signature
from .quote_adapter import QuoteAdapter

DEFAULT_REGISTRY_PATH = Path("artifacts/paypal-sandbox-qualification/merchant-qualification.json")


def _redacted_case_dict(case: Case) -> dict[str, Any]:
    """Return a secret-free view of a Case for reports and fixtures."""
    base = {
        "case_id": case.case_id,
        "run_id": case.run_id,
        "merchant_country": case.merchant_country,
        "buyer_country": case.buyer_country,
        "amount": case.amount,
        "currency": case.currency,
        "product_id": case.product_id,
        "variant_id": case.variant_id,
        "status": case.status.value,
        "create_attempts": case.create_attempts,
        "capture_attempts": case.capture_attempts,
        "observed_payer_country": case.observed_payer_country,
        "expected_payer_region": case.expected_payer_region,
        "expected_surcharge_components": case.expected_surcharge_components,
        "expected_surcharge_amount": case.expected_surcharge_amount,
        "paypal_issue": case.paypal_issue,
        "reconciliation": case.reconciliation,
    }
    evidence = case.paypal_evidence or {}
    if evidence:
        base["paypal_evidence"] = {
            "status": evidence.get("status"),
            "gross_amount": evidence.get("gross_amount"),
            "paypal_fee": evidence.get("paypal_fee"),
            "net_amount": evidence.get("net_amount"),
            "payer_country": evidence.get("payer_country"),
        }
    return base


def default_qualification_path() -> Path:
    return DEFAULT_REGISTRY_PATH


def load_qualification_registry(path: Path | None = None) -> dict[str, Any]:
    """Load the merchant-qualification registry, creating a default empty one if needed."""
    if path is None:
        path = default_qualification_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_qualification_registry(registry: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        path = default_qualification_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True))


def is_merchant_excluded(merchant: str, registry: dict[str, Any], override: bool = False) -> bool:
    """Return True if the merchant is excluded from normal public-rate validation."""
    if override:
        return False
    entry = registry.get(merchant.upper())
    if not entry:
        return False
    return entry.get("status") != QualificationStatus.REPRESENTATIVE


def _observation_from_case(case: Case) -> dict[str, Any] | None:
    evidence = case.paypal_evidence or {}
    if evidence.get("status") != "COMPLETED":
        return None
    gross = evidence.get("gross_amount", {})
    fee = evidence.get("paypal_fee", {})
    if not gross.get("value") or not fee.get("value"):
        return None
    return {
        "amount": gross.get("value"),
        "currency": gross.get("currency_code"),
        "paypal_fee": fee.get("value"),
        "buyer_country": case.buyer_country,
        "observed_payer_country": evidence.get("payer_country"),
        "case_id": case.case_id,
    }


def _library_observations_for_buyers(
    merchant_country: str,
    buyers: set[str],
    amount: str,
    currency: str,
    adapter: QuoteAdapter,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for buyer in buyers:
        try:
            quote = adapter.build_quote(merchant_country, buyer, amount, currency)
        except Exception:
            continue
        if quote.get("status") != "exact_for_public_rate":
            continue
        fee = quote.get("processing_fee", {})
        observations.append(
            {
                "amount": amount,
                "currency": currency,
                "paypal_fee": fee.get("value"),
                "buyer_country": buyer,
                "observed_payer_country": buyer,
            }
        )
    return observations


def classify_qualification(
    merchant_country: str,
    cases: list[Case],
    adapter: QuoteAdapter,
    account_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a merchant based on its bounded qualification captures."""
    observations = [_observation_from_case(c) for c in cases]
    observations = [o for o in observations if o]

    result: dict[str, Any] = {
        "merchant_country": merchant_country,
        "status": QualificationStatus.INCONCLUSIVE,
        "reason": "No completed qualification observations.",
        "observations": observations,
        "account_configuration": account_config,
    }

    if not observations:
        return result

    # Validate each observation before using it.
    validation_failures: list[dict[str, Any]] = []
    for case in cases:
        validation = validate_case_constraints(case)
        if not validation["valid"]:
            validation_failures.append({"case_id": case.case_id, "classification": validation["classification"]})
    if validation_failures:
        return {
            **result,
            "status": QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED,
            "reason": "One or more qualification captures failed validation.",
            "validation_failures": validation_failures,
        }

    # Verify the library was able to produce a calculable quote for every case.
    incalculable = [c.case_id for c in cases if not c.quote or c.quote.get("status") != "exact_for_public_rate"]
    if incalculable:
        return {
            **result,
            "status": QualificationStatus.DATASET_NOT_CALCULABLE,
            "reason": f"Library could not calculate {len(incalculable)} qualification case(s).",
            "incalculable_cases": incalculable,
        }

    # Build library predictions for the same buyers and amount.
    first = observations[0]
    amount = first["amount"]
    currency = first["currency"]
    buyers = {o["buyer_country"] for o in observations}
    library_observations = _library_observations_for_buyers(merchant_country, buyers, amount, currency, adapter)
    library_formula = infer_formula(library_observations)
    paypal_formula = infer_formula(observations)
    result["paypal_formula"] = paypal_formula
    result["library_formula"] = library_formula

    paypal_by_buyer: dict[str, Decimal] = {}
    library_by_buyer: dict[str, Decimal] = {}
    for o in observations:
        paypal_by_buyer[o["buyer_country"]] = _decimal(o["paypal_fee"]) or Decimal("0")
    for o in library_observations:
        library_by_buyer[o["buyer_country"]] = _decimal(o["paypal_fee"]) or Decimal("0")

    # Representative: every tested buyer matches the published public schedule.
    all_match = True
    matched_buyers = 0
    for buyer, paypal_fee in paypal_by_buyer.items():
        lib_fee = library_by_buyer.get(buyer)
        if lib_fee is None:
            all_match = False
            continue
        matched_buyers += 1
        if paypal_fee.quantize(Decimal("0.01")) != lib_fee.quantize(Decimal("0.01")):
            all_match = False

    if all_match and matched_buyers == len(paypal_by_buyer) and matched_buyers >= 2:
        return {
            **result,
            "status": QualificationStatus.REPRESENTATIVE,
            "reason": "Observed PayPal fees match the library's public formula for tested buyers.",
        }

    # Sandbox-specific pricing: PayPal ignores payer-region schedule differences.
    if len(buyers) >= 2:
        unique_paypal_fees = set(paypal_by_buyer.values())
        unique_library_fees = set(library_by_buyer.values())
        paypal_constant = len(unique_paypal_fees) == 1
        library_varies = len(unique_library_fees) > 1

        if paypal_constant and library_varies:
            return {
                **result,
                "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING,
                "reason": (
                    "Sandbox account or Sandbox pricing behavior is not representative of the "
                    "published public fee schedule. PayPal applied the same fee across tested "
                    "payer regions while the library predicted schedule-based differences."
                ),
            }

        if merchant_country in buyers:
            domestic_paypal = paypal_by_buyer.get(merchant_country)
            domestic_lib = library_by_buyer.get(merchant_country)
            foreign_buyers = [b for b in buyers if b != merchant_country]
            if domestic_paypal is not None and domestic_lib is not None and domestic_paypal == domestic_lib:
                foreign_match = all(paypal_by_buyer.get(b) == domestic_paypal for b in foreign_buyers)
                foreign_lib_differs = any(
                    library_by_buyer.get(b) != domestic_lib for b in foreign_buyers if b in library_by_buyer
                )
                if foreign_match and foreign_lib_differs:
                    return {
                        **result,
                        "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING,
                        "reason": (
                            "Domestic fee matches public schedule, but international surcharges are "
                            "not applied by the Sandbox account. Sandbox account is not representative."
                        ),
                    }

    return {
        **result,
        "status": QualificationStatus.INCONCLUSIVE,
        "reason": "PayPal observations do not clearly match or clearly contradict the public schedule.",
    }


def _qualification_amount(merchant_country: str) -> str:
    if currency_for_country(merchant_country) == "JPY":
        return "1000"
    return "10.00"


def _select_qualification_buyers(
    merchant_country: str,
    buyer_countries: set[str],
    adapter: QuoteAdapter,
    amount: str,
    currency: str,
) -> list[str]:
    """Select up to two foreign buyers with distinct fee signatures."""
    if merchant_country not in buyer_countries:
        return []

    domestic_quote: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        domestic_quote = adapter.build_quote(merchant_country, merchant_country, amount, currency)
    domestic_signature = _quote_signature(domestic_quote)

    selected: list[str] = []
    seen_signatures: set[str] = {domestic_signature}
    for buyer in sorted(buyer_countries):
        if buyer == merchant_country or buyer in selected:
            continue
        try:
            quote = adapter.build_quote(merchant_country, buyer, amount, currency)
        except Exception:
            continue
        if quote.get("status") != "exact_for_public_rate":
            continue
        signature = _quote_signature(quote)
        if signature not in seen_signatures or not selected:
            selected.append(buyer)
            seen_signatures.add(signature)
        if len(selected) >= 2:
            break
    return selected


def build_qualification_plan(
    run_id: str,
    merchants: list[str],
    buyer_countries: set[str],
    adapter: QuoteAdapter,
    max_cases_per_merchant: int = 3,
) -> list[Case]:
    """Build a bounded qualification plan: domestic + up to two distinct foreign buyers."""
    plan: list[Case] = []
    for merchant in merchants:
        currency = currency_for_country(merchant)
        amount = _qualification_amount(merchant)
        index = 0

        # Domestic case.
        try:
            domestic_quote = adapter.build_quote(merchant, merchant, amount, currency)
        except Exception:
            domestic_quote = None
        index += 1
        plan.append(
            _case_from_quote(
                run_id=run_id,
                case_id=f"qual-{merchant}-{merchant}-{index}",
                merchant_country=merchant,
                buyer_country=merchant,
                quote=domestic_quote or {},
                rationale="qualification_domestic",
            )
        )

        # Foreign controls.
        foreign_buyers = _select_qualification_buyers(merchant, buyer_countries, adapter, amount, currency)
        for buyer in foreign_buyers[: max_cases_per_merchant - 1]:
            index += 1
            try:
                quote = adapter.build_quote(merchant, buyer, amount, currency)
            except Exception:
                quote = None
            plan.append(
                _case_from_quote(
                    run_id=run_id,
                    case_id=f"qual-{merchant}-{buyer}-{index}",
                    merchant_country=merchant,
                    buyer_country=buyer,
                    quote=quote or {},
                    rationale="qualification_foreign_control",
                )
            )

    return plan


def build_validation_plan(
    run_id: str,
    qualified_merchants: list[str],
    buyer_countries: set[str],
    adapter: QuoteAdapter,
) -> list[Case]:
    """Build a validation plan with one domestic and one distinct/surcharge case per merchant."""
    plan: list[Case] = []
    for merchant in qualified_merchants:
        currency = currency_for_country(merchant)
        amount = _qualification_amount(merchant)

        try:
            domestic_quote = adapter.build_quote(merchant, merchant, amount, currency)
        except Exception:
            domestic_quote = None
        plan.append(
            _case_from_quote(
                run_id=run_id,
                case_id=f"val-{merchant}-{merchant}-1",
                merchant_country=merchant,
                buyer_country=merchant,
                quote=domestic_quote or {},
                rationale="validation_domestic",
            )
        )

        # Prefer a foreign buyer with a distinct schedule and a surcharge.
        foreign_buyers = _select_qualification_buyers(merchant, buyer_countries, adapter, amount, currency)
        selected: str | None = None
        for buyer in foreign_buyers:
            try:
                quote = adapter.build_quote(merchant, buyer, amount, currency)
            except Exception:
                continue
            if quote.get("status") == "exact_for_public_rate":
                meta = quote.get("_schedule_metadata") or {}
                if meta.get("surcharge_percentage") is not None:
                    selected = buyer
                    break
        if not selected and foreign_buyers:
            selected = foreign_buyers[0]

        if selected:
            try:
                quote = adapter.build_quote(merchant, selected, amount, currency)
            except Exception:
                quote = None
            plan.append(
                _case_from_quote(
                    run_id=run_id,
                    case_id=f"val-{merchant}-{selected}-2",
                    merchant_country=merchant,
                    buyer_country=selected,
                    quote=quote or {},
                    rationale="validation_distinct_schedule",
                )
            )

    return plan


def qualification_summary(
    qualifications: dict[str, Any],
    attempted_merchants: set[str] | None = None,
) -> dict[str, Any]:
    """Aggregate counts from a qualification registry."""
    statuses = [q.get("status") for q in qualifications.values()]
    return {
        "merchants_in_registry": len(qualifications),
        "merchants_attempted": len(attempted_merchants) if attempted_merchants else len(qualifications),
        "merchants_representative": statuses.count(QualificationStatus.REPRESENTATIVE),
        "merchants_sandbox_specific": statuses.count(QualificationStatus.SANDBOX_SPECIFIC_PRICING),
        "merchants_blocked": statuses.count(QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED),
        "merchants_not_calculable": statuses.count(QualificationStatus.DATASET_NOT_CALCULABLE),
        "merchants_capability_unavailable": statuses.count(QualificationStatus.CAPABILITY_UNAVAILABLE),
        "merchants_inconclusive": statuses.count(QualificationStatus.INCONCLUSIVE),
    }


def save_qualification_report(
    run_id: str,
    qualifications: dict[str, Any],
    cases: list[Case],
    output_dir: Path | None = None,
    attempted_merchants: set[str] | None = None,
) -> dict[str, Path]:
    """Write merchant-qualification.json and merchant-qualification.md."""
    from .persistence import run_dir

    if output_dir is None:
        output_dir = run_dir(run_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = qualification_summary(qualifications, attempted_merchants=attempted_merchants)
    report = {
        "run_id": run_id,
        "summary": summary,
        "qualifications": qualifications,
        "cases": [_redacted_case_dict(c) for c in cases],
    }
    json_path = output_dir / "merchant-qualification.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    md_path = output_dir / "merchant-qualification.md"
    md_path.write_text(_render_qualification_markdown(report))
    return {"json": json_path, "md": md_path}


def validation_summary(cases: list[Case], registry: dict[str, Any]) -> dict[str, Any]:
    """Aggregate counts from a validation run."""
    completed = [c for c in cases if c.status == CaseStatus.RECONCILED]
    representative_merchants = {
        c.merchant_country
        for c in cases
        if registry.get(c.merchant_country, {}).get("status") == QualificationStatus.REPRESENTATIVE
    }
    domestic_matches = 0
    surcharge_matches = 0
    fee_mismatches = 0
    for c in completed:
        rec = c.reconciliation or {}
        if rec.get("status") == "match":
            if c.buyer_country == c.merchant_country:
                domestic_matches += 1
            else:
                surcharge_matches += 1
        elif rec.get("status") in {"fee_mismatch", "net_amount_mismatch"}:
            fee_mismatches += 1
    return {
        "representative_merchants": len(representative_merchants),
        "captures_completed": len(completed),
        "domestic_matches": domestic_matches,
        "surcharge_matches": surcharge_matches,
        "fee_mismatches": fee_mismatches,
    }


def save_validation_report(
    run_id: str,
    cases: list[Case],
    registry: dict[str, Any],
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Write regional-validation.json and regional-validation.md."""
    from .persistence import run_dir

    if output_dir is None:
        output_dir = run_dir(run_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = validation_summary(cases, registry)
    per_merchant: dict[str, Any] = {}
    for c in cases:
        per_merchant.setdefault(c.merchant_country, []).append(_redacted_case_dict(c))

    report = {
        "run_id": run_id,
        "summary": summary,
        "qualifications": {k: v for k, v in registry.items() if v.get("status") == QualificationStatus.REPRESENTATIVE},
        "merchant_cases": per_merchant,
    }
    json_path = output_dir / "regional-validation.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    md_path = output_dir / "regional-validation.md"
    md_path.write_text(_render_validation_markdown(report))
    return {"json": json_path, "md": md_path}


def _render_validation_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# PayPal Sandbox Regional Validation Report",
        "",
        f"* Run ID: `{report['run_id']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"* {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Representative merchant cases",
            "",
            "| Merchant | Case | Buyer | Status | Reconciliation |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for merchant, cases in sorted(report["merchant_cases"].items()):
        for c in cases:
            rec = c.get("reconciliation") or {}
            lines.append(
                f"| {merchant} | `{c['case_id']}` | {c['buyer_country']} | {c['status']} | {rec.get('status')} |"
            )
    lines.append("")
    return "\n".join(lines)


def _case_observation_fixture(case: Case) -> dict[str, Any] | None:
    """Build a secret-free positive observation fixture from a completed validation case."""
    if case.status != CaseStatus.RECONCILED:
        return None
    rec = case.reconciliation or {}
    if rec.get("status") != "match":
        return None
    validation = validate_case_constraints(case)
    if not validation["valid"]:
        return None

    registry = load_qualification_registry()
    if registry.get(case.merchant_country, {}).get("status") != QualificationStatus.REPRESENTATIVE:
        return None

    quote = case.quote or {}
    meta = quote.get("_schedule_metadata") or {}
    evidence = case.paypal_evidence or {}
    fee = evidence.get("paypal_fee", {})
    gross = evidence.get("gross_amount", {})
    return {
        "provider": "paypal",
        "environment": "sandbox",
        "merchant_country": case.merchant_country,
        "buyer_country": case.buyer_country,
        "observed_payer_country": evidence.get("payer_country"),
        "amount": {"value": gross.get("value") or case.amount, "currency": gross.get("currency_code") or case.currency},
        "paypal_fee": {"value": fee.get("value"), "currency": fee.get("currency_code") or case.currency},
        "expected_library_fee": {"value": quote.get("processing_fee", {}).get("value"), "currency": case.currency},
        "product_id": case.product_id,
        "variant_id": case.variant_id,
        "payer_region": meta.get("payer_region"),
        "rule_ids": [r.get("rule_id") for r in quote.get("matched_rules", [])],
        "schedule_ids": [
            meta.get("fixed_fee_schedule_id"),
            meta.get("international_surcharge_schedule_id"),
        ],
        "data_revision": quote.get("data", {}).get("content_sha256"),
        "crawler_revision": quote.get("data", {}).get("data_ref"),
        "result": "match",
        "validation_run_id": case.run_id,
    }


def promote_observation_fixtures(
    cases: list[Case],
    diagnostics_dir: Path | None = None,
    fixtures_dir: Path | None = None,
) -> list[Path]:
    """Write secret-free positive fixtures for representative matches.

    Non-promotable cases are written to a diagnostics sub-directory for separate
    review.
    """
    if fixtures_dir is None:
        fixtures_dir = Path("artifacts/paypal-sandbox-observations")
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    if diagnostics_dir is None:
        diagnostics_dir = fixtures_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for case in cases:
        fixture = _case_observation_fixture(case)
        if fixture:
            path = fixtures_dir / f"{case.run_id}-{case.case_id}.json"
            path.write_text(json.dumps(fixture, indent=2, sort_keys=True))
            paths.append(path)
        else:
            # Preserve non-matching observations separately for diagnostics.
            minimal = {
                "provider": "paypal",
                "environment": "sandbox",
                "merchant_country": case.merchant_country,
                "buyer_country": case.buyer_country,
                "amount": {"value": case.amount, "currency": case.currency},
                "status": case.status.value,
                "reconciliation_status": (case.reconciliation or {}).get("status"),
                "paypal_error": case.paypal_error,
                "validation_run_id": case.run_id,
            }
            path = diagnostics_dir / f"{case.run_id}-{case.case_id}.json"
            path.write_text(json.dumps(minimal, indent=2, sort_keys=True))
            paths.append(path)
    return paths


def _render_qualification_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# PayPal Sandbox Merchant Qualification Report",
        "",
        f"* Run ID: `{report['run_id']}`",
        "",
        "## Summary",
        "",
    ]
    summary = report["summary"]
    for key, value in summary.items():
        lines.append(f"* {key}: `{value}`")
    lines.extend(["", "## Per-merchant qualification", "", "| Merchant | Status | Reason |", "| --- | --- | --- |"])
    for merchant, q in sorted(report["qualifications"].items()):
        reason = q.get("reason", "").replace("|", "\\|")
        lines.append(f"| {merchant} | `{q.get('status')}` | {reason} |")
    lines.append("")
    return "\n".join(lines)
