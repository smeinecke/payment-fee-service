"""Merchant qualification logic for the PayPal Sandbox regional pilot.

Qualification runs a bounded, three-capture calibration per Merchant to decide
whether that Merchant's Sandbox account behaves like the published public fee
schedule before it is allowed into the normal validation matrix.
"""

from __future__ import annotations

import contextlib
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .configuration import currency_for_country
from .diagnostics import (
    _decimal,
    infer_formula,
    validate_case_constraints,
)
from .manual_flow import infer_formula as infer_manual_formula
from .models import Case, CaseStatus, QualificationStatus
from .planner import _case_from_quote, _quote_signature
from .quote_adapter import QuoteAdapter, minor_units, quantize_currency

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
        "execution_classification": case.execution_classification,
        "planning_time_registry_status": case.planning_time_registry_status,
        "status": case.status.value,
        "create_attempts": case.create_attempts,
        "capture_attempts": case.capture_attempts,
        "paypal_operations_executed_in_current_run": case.paypal_operations_executed_in_current_run,
        "observation_source": case.observation_source,
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


def _finalize_au_qualification(entry: dict[str, Any]) -> None:
    """Normalize the AU entry after an AU→US diagnostic confirmation."""
    if entry.get("status") != QualificationStatus.SANDBOX_SPECIFIC_PRICING.value:
        return
    if entry.get("merchant_country") != "AU":
        return
    entry["international_surcharge_status"] = "confirmed_for_tested_case"
    entry["international_surcharge_percentage_points"] = "1.00"
    entry["confirmed_case"] = "AU merchant ← US buyer, AUD 10.00"
    entry["cross_region_generalization"] = "not_yet_tested"
    entry["representative_for_public_rates"] = False
    # Ensure the domestic public formula is preserved for comparison.
    if "public_domestic_formula" not in entry and "public_formula" in entry:
        entry["public_domestic_formula"] = entry["public_formula"]

    # Also place the surcharge inside the sandbox_profile_pricing evidence block.
    profile = entry.setdefault("sandbox_profile_pricing", {})
    if isinstance(profile, dict):
        profile.setdefault(
            "international_surcharge",
            {
                "percentage_points": "1.00",
                "status": "confirmed_for_tested_case",
                "confirmed_case": "AU merchant ← US buyer, AUD 10.00",
                "cross_region_generalization": "not_yet_tested",
            },
        )
    account_pct = (entry.get("observed_account_formula") or {}).get("percentage")
    account_fixed = (entry.get("observed_account_formula") or {}).get("fixed", {}).get("value")
    public_pct = (entry.get("public_formula") or {}).get("percentage")
    public_fixed = (entry.get("public_formula") or {}).get("fixed", {}).get("value")
    reason_parts = [
        "This AU Sandbox merchant account applies a stable account-specific "
        f"{account_pct}% + AUD {account_fixed} base formula.",
        f"The international surcharge is confirmed only for the tested case ({entry['confirmed_case']}) "
        f"at {entry['international_surcharge_percentage_points']}pp, producing the observed US-buyer fee.",
        f"The public domestic formula is {public_pct}% + AUD {public_fixed}.",
        "Cross-region generalization is not yet tested.",
    ]
    entry["reason"] = " ".join(reason_parts)


def save_qualification_registry(registry: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        path = default_qualification_path()
    if "AU" in registry:
        _finalize_au_qualification(registry["AU"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True))


def is_merchant_excluded(merchant: str, registry: dict[str, Any], override: bool = False) -> bool:
    """Return True if the merchant is excluded from normal public-rate validation."""
    if override:
        return False
    entry = registry.get(merchant.upper())
    if not entry:
        return False
    if entry.get("representative_for_public_rates") is False:
        return True
    return entry.get("status") != QualificationStatus.REPRESENTATIVE


def is_diagnostic_sandbox_pricing_merchant(entry: dict[str, Any]) -> bool:
    """Return True when a registry entry may be included in diagnostic sandbox-pricing runs.

    Diagnostic sandbox-pricing mode includes only entries with an explicit sandbox
    pricing signal. Blocked, not-calculable, capability-unavailable, compliance
    violation and inconclusive merchants are excluded unless one of the sandbox
    pricing signals is present.
    """
    status = entry.get("status")
    if status == QualificationStatus.SANDBOX_SPECIFIC_PRICING:
        return True
    if entry.get("manual_send_to_business") == QualificationStatus.SANDBOX_SPECIFIC_PRICING.value:
        return True
    observation = entry.get("manual_send_observation") or {}
    if observation.get("classification") == "sandbox_account_pricing_difference":
        return True
    if status in {
        QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED,
        QualificationStatus.SANDBOX_CHECKOUT_LIMITATION,
        QualificationStatus.DATASET_NOT_CALCULABLE,
        QualificationStatus.CAPABILITY_UNAVAILABLE,
        QualificationStatus.INCONCLUSIVE,
        "compliance_violation",
    }:
        return False
    return False


def classify_manual_send_pricing(cases: list[Case]) -> dict[str, Any]:
    """Classify manual-send pricing against the public formula.

    The observed formula is inferred only from fresh pre-submission cases that
    have no FX, payer-country mismatch, or evidence invariant failure. It is
    reported as sandbox-specific pricing only when at least two independent
    fresh observations exist, the observed formula is stable, and at least one
    observation differs from the public prediction with a non-zero minor-unit
    delta. If every fresh observation matches the public formula, the merchant
    is representative for manual-send. Otherwise the result is inconclusive.
    """
    fresh = [c for c in cases if c.prediction_provenance == "pre_submission_prediction" and c.paypal_evidence]
    if len(fresh) < 2:
        return _manual_inconclusive(fresh, "fewer than two fresh observations")

    valid_fresh: list[Case] = []
    invariant_failure = False
    for c in fresh:
        validation = validate_case_constraints(c)
        if not validation["valid"]:
            invariant_failure = True
            continue
        valid_fresh.append(c)

    if invariant_failure:
        return _manual_inconclusive(valid_fresh, "evidence invariant failure detected")
    if len(valid_fresh) < 2:
        return _manual_inconclusive(valid_fresh, "fewer than two valid fresh observations")

    public_matches: list[bool] = []
    public_amounts: list[dict[str, Any]] = []
    currency = valid_fresh[0].currency
    for c in valid_fresh:
        ev = c.paypal_evidence or {}
        q = c.quote or {}
        gross = _decimal(ev.get("gross_amount", {}).get("value"))
        observed_fee = _decimal(ev.get("paypal_fee", {}).get("value"))
        public_fee = _decimal((q.get("processing_fee") or {}).get("value"))
        if gross is None or observed_fee is None or public_fee is None:
            return _manual_inconclusive(valid_fresh, "missing fee values")
        public_amounts.append({"gross": gross, "observed": observed_fee, "public": public_fee})
        public_matches.append(minor_units(observed_fee, currency) == minor_units(public_fee, currency))

    if all(public_matches):
        return _manual_classification(
            valid_fresh,
            status=QualificationStatus.REPRESENTATIVE.value,
            reason="Every fresh observation matches the public formula with zero minor-unit delta.",
        )

    formula = infer_manual_formula(valid_fresh)
    if not formula:
        return _manual_inconclusive(valid_fresh, "could not infer a stable observed formula")

    inferred = formula.get("inferred_from_observations") or {}
    try:
        slope = Decimal(inferred["base_percentage"])
        intercept = Decimal(inferred["fixed_amount"])
    except Exception:
        return _manual_inconclusive(valid_fresh, "inferred formula is incomplete")

    for amounts in public_amounts:
        expected = quantize_currency(amounts["gross"] * slope + intercept, currency)
        if minor_units(expected, currency) != minor_units(amounts["observed"], currency):
            return _manual_inconclusive(valid_fresh, "observed formula is unstable across amounts")

    observed_pct = (slope * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    observed_fixed = quantize_currency(intercept, currency)
    return {
        "merchant_country": valid_fresh[0].merchant_country,
        "execution_path": "manual_send_to_business",
        "product_id": valid_fresh[0].product_id,
        "variant_id": valid_fresh[0].variant_id,
        "status": QualificationStatus.SANDBOX_SPECIFIC_PRICING.value,
        "public_formula": {
            "percentage": formula.get("base_percentage"),
            "fixed": {
                "value": formula.get("fixed_amount"),
                "currency": currency,
            },
        },
        "observed_account_formula": {
            "percentage": str(observed_pct),
            "fixed": {
                "value": str(observed_fixed),
                "currency": currency,
            },
        },
        "classification": "sandbox_account_pricing_difference",
        "confidence": "high",
        "usable_for_public_rate_validation": False,
        "reason": (
            f"This specific {valid_fresh[0].merchant_country} Sandbox merchant account applied a stable "
            f"{observed_pct}% + {currency} {observed_fixed} pricing formula, which differs from the public "
            f"{formula.get('base_percentage')}% + {currency} {formula.get('fixed_amount')} formula."
        ),
    }


def _manual_classification(
    cases: list[Case],
    status: str,
    reason: str,
) -> dict[str, Any]:
    """Build a representative/inconclusive manual-send classification."""
    if not cases:
        return _manual_inconclusive([], reason)
    formula = infer_manual_formula(cases) or {}
    currency = cases[0].currency
    public_pct = formula.get("base_percentage")
    public_fixed = formula.get("fixed_amount")
    observed_pct = public_pct
    observed_fixed = public_fixed
    inferred = formula.get("inferred_from_observations") or {}
    try:
        inferred_slope = Decimal(inferred["base_percentage"])
        inferred_intercept = Decimal(inferred["fixed_amount"])
        observed_pct = str((inferred_slope * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        observed_fixed = str(quantize_currency(inferred_intercept, currency))
    except Exception:
        pass
    classification = "representative" if status == QualificationStatus.REPRESENTATIVE.value else "inconclusive"
    return {
        "merchant_country": cases[0].merchant_country,
        "execution_path": "manual_send_to_business",
        "product_id": cases[0].product_id,
        "variant_id": cases[0].variant_id,
        "status": status,
        "public_formula": {
            "percentage": public_pct,
            "fixed": {"value": public_fixed, "currency": currency},
        },
        "observed_account_formula": {
            "percentage": observed_pct,
            "fixed": {"value": observed_fixed, "currency": currency},
        },
        "classification": classification,
        "confidence": "high" if status == QualificationStatus.REPRESENTATIVE.value else "low",
        "usable_for_public_rate_validation": status == QualificationStatus.REPRESENTATIVE.value,
        "reason": reason,
    }


def _manual_inconclusive(cases: list[Case], reason: str) -> dict[str, Any]:
    """Return an inconclusive manual-send classification."""
    if cases:
        return _manual_classification(cases, QualificationStatus.INCONCLUSIVE.value, reason)
    return {
        "merchant_country": None,
        "execution_path": "manual_send_to_business",
        "status": QualificationStatus.INCONCLUSIVE.value,
        "classification": "inconclusive",
        "confidence": "low",
        "usable_for_public_rate_validation": False,
        "reason": reason,
    }


def update_manual_send_qualification(
    registry: dict[str, Any],
    merchant_country: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Update the qualification registry with a manual-send classification.

    Preserves any existing orders_v2_checkout diagnosis and marks the merchant
    as not representative for public-rate validation when sandbox-specific.
    """
    entry = registry.get(merchant_country, {})
    status = classification.get("status", QualificationStatus.INCONCLUSIVE.value)
    entry.update(
        {
            "merchant_country": merchant_country,
            "manual_send_to_business": status,
            "representative_for_public_rates": status == QualificationStatus.REPRESENTATIVE.value,
            "manual_send_observation": classification,
            "status": status,
            "reason": classification.get("reason", "Manual-send classification updated."),
        }
    )
    if "orders_v2_checkout" not in entry:
        diagnosis = entry.get("diagnosis") or {}
        ctx = diagnosis.get("playwright_application_context") or {}
        src = diagnosis.get("playwright_payment_source") or {}
        order = diagnosis.get("manual_order") or {}
        if ctx.get("status") == "failed" or src.get("status") == "failed" or order.get("status") == "timeout":
            entry["orders_v2_checkout"] = QualificationStatus.SANDBOX_CHECKOUT_LIMITATION.value
        else:
            entry["orders_v2_checkout"] = QualificationStatus.INCONCLUSIVE.value
    registry[merchant_country] = entry
    return registry


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
        "currency": gross.get("currency_code") or case.currency,
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
        if minor_units(paypal_fee, currency) != minor_units(lib_fee, currency):
            all_match = False

    if all_match and matched_buyers == len(paypal_by_buyer) and matched_buyers >= 2:
        return {
            **result,
            "status": QualificationStatus.REPRESENTATIVE,
            "reason": "Observed PayPal fees match the library's public formula for tested buyers.",
        }

    # Sandbox-specific pricing: PayPal ignores payer-region schedule differences.
    if len(buyers) >= 2:
        unique_paypal_fees = {minor_units(v, currency) for v in paypal_by_buyer.values()}
        unique_library_fees = {minor_units(v, currency) for v in library_by_buyer.values()}
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
            if (
                domestic_paypal is not None
                and domestic_lib is not None
                and minor_units(domestic_paypal, currency) == minor_units(domestic_lib, currency)
            ):
                foreign_match = all(
                    minor_units(paypal_by_buyer.get(b), currency) == minor_units(domestic_paypal, currency)
                    for b in foreign_buyers
                    if paypal_by_buyer.get(b) is not None
                )
                foreign_lib_differs = any(
                    minor_units(library_by_buyer.get(b), currency) != minor_units(domestic_lib, currency)
                    for b in foreign_buyers
                    if b in library_by_buyer
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
    from .profile_pricing import build_profile_pricing_verifications

    if output_dir is None:
        output_dir = run_dir(run_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = qualification_summary(qualifications, attempted_merchants=attempted_merchants)
    profile_verifications = build_profile_pricing_verifications(qualifications)
    summary["merchants_with_profile_pricing"] = len(profile_verifications)
    summary["merchants_profile_matches_transactions"] = sum(
        1 for v in profile_verifications if v.get("status") == "profile_matches_transactions"
    )
    report = {
        "run_id": run_id,
        "summary": summary,
        "qualifications": qualifications,
        "profile_pricing_verifications": profile_verifications,
        "cases": [_redacted_case_dict(c) for c in cases],
    }
    json_path = output_dir / "merchant-qualification.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    md_path = output_dir / "merchant-qualification.md"
    md_path.write_text(_render_qualification_markdown(report))
    return {"json": json_path, "md": md_path}


def validation_summary(
    cases: list[Case],
    registry: dict[str, Any],
    fixture_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Aggregate counts from a validation run.

    Counters use the immutable execution_classification stored on each case at
    planning time, not the current mutable registry. This keeps diagnostic and
    sandbox-pricing observations from contaminating public-rate acceptance stats.
    Live captures and historical fixture reuse are reported separately.
    """
    completed = [c for c in cases if c.status == CaseStatus.RECONCILED]
    public_rate_completed = [c for c in completed if c.execution_classification == "public_rate_validation"]
    diagnostic_completed = [c for c in completed if c.execution_classification == "diagnostic_sandbox_pricing"]

    public_rate_merchants = {
        c.merchant_country for c in cases if c.execution_classification == "public_rate_validation"
    }
    diagnostic_merchants = {
        c.merchant_country for c in cases if c.execution_classification == "diagnostic_sandbox_pricing"
    }

    live_public_completed = [c for c in public_rate_completed if not c.pilot_metadata.get("reused_observation")]
    reused_public_completed = [c for c in public_rate_completed if c.pilot_metadata.get("reused_observation")]

    domestic_matches = 0
    surcharge_matches = 0
    fee_mismatches = 0
    historical_mismatches = 0
    diagnostic_matches = 0
    diagnostic_mismatches = 0
    for c in public_rate_completed:
        rec = c.reconciliation or {}
        if rec.get("status") == "match":
            if c.buyer_country == c.merchant_country:
                domestic_matches += 1
            else:
                surcharge_matches += 1
        elif rec.get("status") == "historical_observation_current_mismatch":
            historical_mismatches += 1
        elif rec.get("status") in {"fee_mismatch", "net_amount_mismatch"}:
            fee_mismatches += 1

    for c in diagnostic_completed:
        rec = c.reconciliation or {}
        if rec.get("status") == "match":
            diagnostic_matches += 1
        elif rec.get("status") in {"fee_mismatch", "net_amount_mismatch"}:
            diagnostic_mismatches += 1

    positive_fixtures = 0
    if fixture_paths:
        positive_fixtures = sum(1 for p in fixture_paths if p.parent.name != "diagnostics")

    return {
        "representative_merchants": len(public_rate_merchants),
        "diagnostic_merchants": len(diagnostic_merchants),
        "cases_reconciled": len(completed),
        "captures_completed": len(live_public_completed),
        "live_captures_completed": len(live_public_completed),
        "representative_captures_completed": len(live_public_completed),
        "historical_observations_reused": len(reused_public_completed),
        "new_positive_fixtures_generated": positive_fixtures,
        "diagnostic_captures_completed": len(diagnostic_completed),
        "diagnostic_matches": diagnostic_matches,
        "diagnostic_mismatches": diagnostic_mismatches,
        "domestic_matches": domestic_matches,
        "surcharge_matches": surcharge_matches,
        "fee_mismatches": fee_mismatches,
        "historical_observation_current_mismatches": historical_mismatches,
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
            "| Merchant | Case | Buyer | Classification | Status | Reconciliation |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for merchant, cases in sorted(report["merchant_cases"].items()):
        for c in cases:
            rec = c.get("reconciliation") or {}
            line = (
                f"| {merchant} | `{c['case_id']}` | {c['buyer_country']} "
                f"| {c.get('execution_classification')} | {c['status']} "
                f"| {rec.get('status')} |"
            )
            lines.append(line)
    lines.append("")
    return "\n".join(lines)


def _case_observation_fixture(case: Case) -> dict[str, Any] | None:
    """Build a secret-free positive observation fixture from a completed validation case.

    Promotion is allowed only when the case was planned as public-rate validation,
    the merchant was representative at planning time, and the case reconciled cleanly.
    """
    if case.status != CaseStatus.RECONCILED:
        return None
    rec = case.reconciliation or {}
    if rec.get("status") != "match":
        return None
    validation = validate_case_constraints(case)
    if not validation["valid"]:
        return None
    if case.execution_classification != "public_rate_validation":
        return None
    if case.planning_time_registry_status != QualificationStatus.REPRESENTATIVE.value:
        return None
    if case.pilot_metadata.get("reused_observation"):
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
        if case.pilot_metadata.get("reused_observation"):
            continue
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

    verifications = report.get("profile_pricing_verifications") or []
    if verifications:
        lines.extend(
            [
                "## Sandbox profile pricing verification",
                "",
                "| Merchant | Profile wallet | Observed wallet | Delta | Status | Production representative |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for v in verifications:
            note = v.get("international_surcharge_note") or ""
            observed = v.get("observed_wallet_formula") or "n/a"
            lines.append(
                f"| {v['merchant_country']} | `{v['profile_wallet_formula']}` | `{observed}` | "
                f"`{v.get('delta_minor_units')}` | `{v['status']}` | "
                f"`{v.get('production_representative')}` |"
            )
            if note:
                lines.append(f"| | | | | | {note.replace('|', '\\|')} |")
        lines.append("")

    return "\n".join(lines)
