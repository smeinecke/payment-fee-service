"""PayPal Sandbox fee-mismatch diagnostics.

This module preserves original evidence, validates case constraints, decomposes
the library calculation, infers candidate fee formulas from additional
observations, and classifies the primary root cause.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import Case
from .persistence import load_case, load_results, run_dir
from .quote_adapter import currency_exponent, minor_units, quantize_currency


def load_original_case(run_id: str, case_id: str) -> Case:
    """Load a previously executed case from its persisted run artifacts."""
    try:
        return load_case(run_id, case_id)
    except FileNotFoundError as exc:
        # Fall back to results.json for older runs.
        results = load_results(run_id)
        for c in results.get("cases", []):
            if c.get("case_id") == case_id:
                return Case.model_validate(c)
        raise ValueError(f"Case {case_id!r} not found in run {run_id}") from exc


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def validate_case_constraints(case: Case) -> dict[str, Any]:
    """Return validation outcome and any early-stop classification."""
    evidence = case.paypal_evidence or {}
    gross = evidence.get("gross_amount", {})
    fee = evidence.get("paypal_fee", {})
    net = evidence.get("net_amount", {})
    observed_country = evidence.get("payer_country")

    result = {
        "valid": True,
        "classification": None,
        "checks": {
            "merchant_country": case.merchant_country,
            "configured_buyer_country": case.buyer_country,
            "observed_payer_country": observed_country,
            "currency": gross.get("currency_code"),
            "capture_status": evidence.get("status"),
        },
    }

    if evidence.get("status") != "COMPLETED":
        result["valid"] = False
        result["classification"] = "paypal_api_failure"
        return result

    if case.buyer_country and observed_country != case.buyer_country:
        result["valid"] = False
        result["classification"] = "buyer_country_mismatch"
        return result

    if gross.get("currency_code") != fee.get("currency_code") or gross.get("currency_code") != net.get("currency_code"):
        result["valid"] = False
        result["classification"] = "excluded_fx_case"
        return result

    gross_value = _decimal(gross.get("value"))
    fee_value = _decimal(fee.get("value"))
    net_value = _decimal(net.get("value"))
    if gross_value is None or fee_value is None or net_value is None:
        result["valid"] = False
        result["classification"] = "harness_evidence_defect"
        return result

    currency = case.currency
    if minor_units(gross_value - fee_value, currency) != minor_units(net_value, currency):
        result["valid"] = False
        result["classification"] = "paypal_api_evidence_invalid"
        return result

    quote = case.quote or {}
    components = quote.get("components", [])
    if components:
        comp_values = [_decimal(c.get("amount")) for c in components]
        comp_total = sum((v for v in comp_values if v is not None), Decimal("0"))
        proc_fee = _decimal(quote.get("processing_fee", {}).get("value"))
        if proc_fee is not None and minor_units(comp_total, currency) != minor_units(proc_fee, currency):
            result["valid"] = False
            result["classification"] = "harness_evidence_defect"
            return result

    return result


def decompose_case(case: Case) -> dict[str, Any]:
    """Produce an explicit Decimal financial decomposition for a case."""
    evidence = case.paypal_evidence or {}
    quote = case.quote or {}
    components = quote.get("components", [])
    meta = quote.get("_schedule_metadata", {})
    currency = case.currency

    gross = _decimal(evidence.get("gross_amount", {}).get("value"))
    paypal_fee = _decimal(evidence.get("paypal_fee", {}).get("value"))
    net = _decimal(evidence.get("net_amount", {}).get("value"))

    processing = next((c for c in components if c.get("type") == "processing"), {})
    surcharge = next((c for c in components if c.get("type") == "surcharge"), {})

    base_pct = _decimal(processing.get("rate_percentage")) or _decimal(meta.get("base_percentage"))
    fixed = _decimal(processing.get("fixed_amount")) or _decimal(meta.get("fixed_amount"))
    surcharge_pct = _decimal(surcharge.get("rate_percentage")) or _decimal(meta.get("surcharge_percentage"))

    library_base_pct_amount = None
    library_base_total = None
    if gross is not None and base_pct is not None:
        library_base_pct_amount = quantize_currency(gross * base_pct / Decimal(100), currency)
        base_with_fixed = (library_base_pct_amount + fixed) if fixed is not None else library_base_pct_amount
        library_base_total = quantize_currency(base_with_fixed, currency)

    library_surcharge_pct_amount = None
    if gross is not None and surcharge_pct is not None:
        library_surcharge_pct_amount = quantize_currency(gross * surcharge_pct / Decimal(100), currency)

    component_unrounded = []
    component_rounded = []
    if gross is not None:
        for c in components:
            amount = _decimal(c.get("amount"))
            rate = _decimal(c.get("rate_percentage"))
            fixed_comp = _decimal(c.get("fixed_amount"))
            unrounded = None
            if amount is not None and rate is not None:
                raw = gross * rate / Decimal(100)
                if fixed_comp is not None:
                    raw += fixed_comp
                unrounded = raw
            rounded = amount
            component_unrounded.append(
                {"type": c.get("type"), "unrounded": str(unrounded) if unrounded is not None else None}
            )
            component_rounded.append({"type": c.get("type"), "rounded": str(rounded) if rounded is not None else None})

    total_before_cap = None
    if library_base_total is not None and library_surcharge_pct_amount is not None:
        total_before_cap = quantize_currency(library_base_total + library_surcharge_pct_amount, currency)
    elif library_base_total is not None:
        total_before_cap = library_base_total

    library_fee = _decimal(quote.get("processing_fee", {}).get("value"))

    min_applied = any(c.get("minimum_applied") for c in components)
    max_applied = any(c.get("maximum_applied") for c in components)

    # Determine rounding point: compare total from per-component rounding vs aggregate.
    rounding_point = "per component"  # default
    if gross is not None and base_pct is not None:
        raw_base = gross * base_pct / Decimal(100)
        if fixed is not None:
            raw_base += fixed
        raw_total = raw_base + (gross * surcharge_pct / Decimal(100)) if surcharge_pct is not None else raw_base
        aggregate = quantize_currency(raw_total, currency)
        if total_before_cap is not None and aggregate == total_before_cap:
            rounding_point = "after component aggregation"

    return {
        "paypal": {
            "gross": str(gross) if gross is not None else None,
            "fee": str(paypal_fee) if paypal_fee is not None else None,
            "net": str(net) if net is not None else None,
        },
        "library_base": {
            "percentage": str(base_pct) if base_pct is not None else None,
            "calculated_percentage_amount": str(library_base_pct_amount)
            if library_base_pct_amount is not None
            else None,
            "direct_fixed_amount": str(fixed) if fixed is not None else None,
            "fixed_schedule_amount": str(fixed) if fixed is not None else None,
            "total": str(library_base_total) if library_base_total is not None else None,
        },
        "library_surcharge": {
            "percentage": str(surcharge_pct) if surcharge_pct is not None else None,
            "calculated_surcharge_amount": str(library_surcharge_pct_amount)
            if library_surcharge_pct_amount is not None
            else None,
            "fixed_surcharge_amount": None,
        },
        "library": {
            "unrounded_component_amounts": component_unrounded,
            "rounded_component_amounts": component_rounded,
            "total_before_cap": str(total_before_cap) if total_before_cap is not None else None,
            "minimum_applied": min_applied,
            "maximum_applied": max_applied,
            "final_fee": str(library_fee) if library_fee is not None else None,
            "rounding_point": rounding_point,
        },
        "product_id": case.product_id,
        "variant_id": case.variant_id,
        "payer_region": meta.get("payer_region")
        or quote.get("_request", {}).get("transaction", {}).get("payer_region"),
        "base_rule_id": meta.get("base_rule_id"),
        "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
        "data_revision": quote.get("data", {}).get("content_sha256"),
        "crawler_revision": (quote.get("data", {}).get("data_ref") or "local"),
    }


def _rounded_fee(
    amount: Decimal,
    percentage: Decimal | None,
    fixed: Decimal | None,
    currency: str,
) -> Decimal:
    raw = Decimal("0")
    if percentage is not None:
        raw += amount * percentage / Decimal(100)
    if fixed is not None:
        raw += fixed
    return quantize_currency(raw, currency)


def _percentage_plus_fixed_candidates(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Infer p*amount + f from all pairs and score against all observations."""
    amounts = [_decimal(o["amount"]) for o in observations]
    fees = [_decimal(o["paypal_fee"]) for o in observations]
    countries = [o.get("buyer_country") for o in observations]
    currency = (observations[0].get("currency") if observations else None) or "USD"

    points = [(a, f) for a, f in zip(amounts, fees, strict=False) if a is not None and f is not None]
    if len(points) < 2:
        return []

    # Use the two largest amounts to reduce rounding noise.
    points.sort(key=lambda x: x[0])
    largest = points[-2:]
    a1, f1 = largest[0]
    a2, f2 = largest[1]
    if a2 == a1:
        return []
    # (f2 - f1) / (a2 - a1) is the fraction; convert to percentage points.
    p = ((f2 - f1) / (a2 - a1) * Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    fixed = quantize_currency(f1 - (p / Decimal(100)) * a1, currency)

    candidate = {
        "formula_type": "percentage_plus_fixed",
        "percentage": str(p),
        "fixed": str(fixed),
        "currency": currency,
        "predictions": [],
        "errors_minor": [],
        "max_error_minor": None,
        "fit": False,
    }
    max_err_minor = 0
    for a, f, country in zip(amounts, fees, countries, strict=False):
        if a is None or f is None:
            continue
        pred = _rounded_fee(a, p, fixed, currency)
        pred_minor = minor_units(pred, currency)
        obs_minor = minor_units(f, currency)
        err_minor = pred_minor - obs_minor
        err = quantize_currency(Decimal(err_minor) / (Decimal(10) ** currency_exponent(currency)), currency)
        candidate["predictions"].append(
            {
                "amount": str(a),
                "buyer_country": country,
                "predicted": str(pred),
                "observed": str(f),
                "error": str(err),
                "error_minor_units": abs(err_minor),
            }
        )
        max_err_minor = max(max_err_minor, abs(err_minor))
        candidate["errors_minor"].append(abs(err_minor))

    candidate["max_error_minor"] = max_err_minor
    candidate["fit"] = all(e == 0 for e in candidate["errors_minor"])
    return [candidate]


def _base_plus_surcharge_candidates(
    observations: list[dict[str, Any]],
    base_pct: Decimal | None,
    surcharge_pct: Decimal | None,
    fixed: Decimal | None,
    currency: str,
) -> list[dict[str, Any]]:
    """Evaluate the library's base + surcharge + fixed model against observations."""
    if base_pct is None or surcharge_pct is None or fixed is None:
        return []

    candidate = {
        "formula_type": "base_plus_surcharge_plus_fixed",
        "base_percentage": str(base_pct),
        "surcharge_percentage": str(surcharge_pct),
        "fixed": str(fixed),
        "currency": currency,
        "predictions": [],
        "errors_minor": [],
        "max_error_minor": None,
        "fit": False,
    }
    max_err_minor = 0
    for o in observations:
        a = _decimal(o["amount"])
        f = _decimal(o["paypal_fee"])
        if a is None or f is None:
            continue
        base_part = quantize_currency(a * base_pct / Decimal(100), currency)
        surcharge_part = quantize_currency(a * surcharge_pct / Decimal(100), currency)
        pred = quantize_currency(base_part + surcharge_part + fixed, currency)
        pred_minor = minor_units(pred, currency)
        obs_minor = minor_units(f, currency)
        err_minor = pred_minor - obs_minor
        err = quantize_currency(Decimal(err_minor) / (Decimal(10) ** currency_exponent(currency)), currency)
        candidate["predictions"].append(
            {
                "amount": str(a),
                "buyer_country": o.get("buyer_country"),
                "predicted": str(pred),
                "observed": str(f),
                "error": str(err),
                "error_minor_units": abs(err_minor),
            }
        )
        max_err_minor = max(max_err_minor, abs(err_minor))
        candidate["errors_minor"].append(abs(err_minor))

    candidate["max_error_minor"] = max_err_minor
    candidate["fit"] = all(e == 0 for e in candidate["errors_minor"])
    return [candidate]


def infer_formula(
    observations: list[dict[str, Any]],
    base_pct: Decimal | None = None,
    surcharge_pct: Decimal | None = None,
    fixed: Decimal | None = None,
) -> dict[str, Any]:
    """Infer PayPal's effective formula from a set of observations.

    Each observation must contain ``amount``, ``paypal_fee``, ``buyer_country``,
    and ``payer_country``.
    """
    currency = (observations[0].get("currency") if observations else None) or "USD"
    candidates = []
    candidates.extend(_percentage_plus_fixed_candidates(observations))
    candidates.extend(_base_plus_surcharge_candidates(observations, base_pct, surcharge_pct, fixed, currency))

    # Pick best candidate: first formula that fits exactly, else lowest max error.
    best = None
    for c in candidates:
        if c.get("fit"):
            best = c
            break
    if best is None and candidates:
        best = min(candidates, key=lambda c: c.get("max_error_minor") or math.inf)

    return {
        "candidates": candidates,
        "best": best,
        "stable_linear_formula_found": best is not None and best.get("fit") is True,
    }


def build_observations_from_run(
    run_id: str, merchant_country: str, buyer_country: str | None = None, currency: str | None = None
) -> list[dict[str, Any]]:
    """Collect secret-free observations from a diagnostic run."""
    results = load_results(run_id)
    observations: list[dict[str, Any]] = []
    for c in results.get("cases", []):
        if c.get("merchant_country") != merchant_country:
            continue
        if buyer_country and c.get("buyer_country") != buyer_country:
            continue
        if currency and c.get("currency") != currency:
            continue
        evidence = c.get("paypal_evidence") or {}
        if evidence.get("status") != "COMPLETED":
            continue
        gross = evidence.get("gross_amount", {})
        fee = evidence.get("paypal_fee", {})
        observations.append(
            {
                "amount": gross.get("value"),
                "currency": gross.get("currency_code"),
                "paypal_fee": fee.get("value"),
                "buyer_country": c.get("buyer_country"),
                "observed_payer_country": evidence.get("payer_country"),
            }
        )
    return observations


def classify_root_cause(
    case: Case, decomposition: dict[str, Any], formula: dict[str, Any], account_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Classify the primary root cause for the mismatch."""
    validation = validate_case_constraints(case)
    if not validation["valid"]:
        return {
            "category": validation["classification"],
            "confidence": "confirmed",
            "explanation": "Case failed pre-flight validation; no fee-model analysis performed.",
            "evidence": validation["checks"],
        }

    best = formula.get("best")
    if best is None:
        return {
            "category": "unknown",
            "confidence": "low",
            "explanation": "Could not infer a stable formula from diagnostic observations.",
        }

    library_pct = decomposition["library_base"]["percentage"]
    library_fixed = decomposition["library_base"]["direct_fixed_amount"]
    library_surcharge_pct = decomposition["library_surcharge"]["percentage"]
    library_total = decomposition["library"]["final_fee"]
    paypal_fee = decomposition["paypal"]["fee"]
    currency = case.currency

    if best["formula_type"] == "percentage_plus_fixed":
        obs_pct = best.get("percentage")
        obs_fixed = best.get("fixed")
    elif best["formula_type"] == "base_plus_surcharge_plus_fixed":
        base_pct_d = _decimal(best.get("base_percentage"))
        surcharge_pct_d = _decimal(best.get("surcharge_percentage"))
        obs_pct = str(base_pct_d + surcharge_pct_d) if base_pct_d is not None and surcharge_pct_d is not None else None
        obs_fixed = best.get("fixed")
    else:
        obs_pct = None
        obs_fixed = None

    # Stable observed formula takes precedence; it pinpoints account config or schedule defects.
    if formula.get("stable_linear_formula_found"):
        # If the observed formula equals the published base-only formula, the
        # surcharge component is missing in PayPal's settlement.
        if obs_pct == library_pct and obs_fixed == library_fixed:
            return {
                "category": "payment_fee_data_defect",
                "confidence": "high",
                "explanation": (
                    "Observed fee matches the library base-only amount; the published "
                    "surcharge schedule is not being applied by PayPal for this merchant."
                ),
            }
        # If the observed total percentage equals the base plus the published surcharge,
        # the surcharge is applied as a single combined rate.
        if (
            obs_pct is not None
            and library_pct is not None
            and library_surcharge_pct is not None
            and _decimal(obs_pct) is not None
            and _decimal(library_pct) is not None
            and _decimal(library_surcharge_pct) is not None
            and _decimal(obs_pct) == _decimal(library_pct) + _decimal(library_surcharge_pct)
            and obs_fixed == library_fixed
        ):
            return {
                "category": "payment_fee_data_defect",
                "confidence": "high",
                "explanation": (
                    "PayPal applied the published base rate plus a 1.00pp surcharge as a single "
                    "combined rate; the surcharge schedule is not itemised separately."
                ),
            }
        # A completely different stable formula strongly suggests a custom/negotiated
        # merchant rate in the Sandbox account.
        return {
            "category": "sandbox_account_configuration",
            "confidence": "high",
            "explanation": (
                f"PayPal applied a stable formula of {obs_pct}% + {obs_fixed} "
                f"instead of the published {library_pct}% + {library_fixed} base and "
                f"{library_surcharge_pct}% surcharge. This is consistent with custom or "
                "negotiated pricing on the Sandbox merchant account."
            ),
        }

    # Without a stable formula, compare the single observed fee against the library components.
    if paypal_fee is not None and library_total is not None:
        paypal_fee_minor = minor_units(paypal_fee, currency)
        base_total_minor = minor_units(decomposition["library_base"]["total"], currency)
        total_minor = minor_units(library_total, currency)

        if (
            base_total_minor is not None
            and paypal_fee_minor == base_total_minor
            and library_surcharge_pct
            and _decimal(library_surcharge_pct)
        ):
            return {
                "category": "payment_fee_data_defect",
                "confidence": "high",
                "explanation": (
                    "Observed fee matches the library base-only amount; the published "
                    "surcharge schedule is not being applied by PayPal for this merchant."
                ),
            }

        if (
            total_minor is not None
            and paypal_fee_minor == total_minor
            and library_surcharge_pct
            and _decimal(library_surcharge_pct)
        ):
            return {
                "category": "payment_fee_data_defect",
                "confidence": "high",
                "explanation": (
                    "PayPal applied the total base-plus-surcharge amount, but the surcharge "
                    "is not itemised separately; it is rolled into the collected fee."
                ),
            }

    # If best candidate is not a perfect fit, but the only candidate is the library formula
    # with an error, it may be a rounding/precision defect.
    if best.get("formula_type") == "base_plus_surcharge_plus_fixed" and not formula.get("stable_linear_formula_found"):
        return {
            "category": "payment_fee_calculation_or_rounding_defect",
            "confidence": "medium",
            "explanation": "The library's base-plus-surcharge formula does not exactly predict the observed fees.",
        }

    return {
        "category": "unknown",
        "confidence": "low",
        "explanation": "No single root cause could be determined from the available observations.",
    }


def generate_diagnostic_reports(
    run_id: str,
    case_id: str,
    case: Case,
    decomposition: dict[str, Any],
    formula: dict[str, Any],
    root_cause: dict[str, Any],
    observations: list[dict[str, Any]],
    account_config: dict[str, Any] | None,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Write secret-free diagnostic artifacts."""
    if output_dir is None:
        output_dir = run_dir(run_id) / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)

    fee_d = _decimal(decomposition["paypal"]["fee"]) or Decimal("0")
    lib_fee_d = _decimal(decomposition["library"]["final_fee"]) or Decimal("0")
    fixed_d = _decimal(decomposition["library_base"]["direct_fixed_amount"]) or Decimal("0")
    amount_d = _decimal(case.amount) or Decimal("0")
    delta = (fee_d - lib_fee_d).copy_abs()

    diagnostic = {
        "run_id": run_id,
        "case_id": case_id,
        "merchant_country": case.merchant_country,
        "buyer_country": case.buyer_country,
        "amount": case.amount,
        "currency": case.currency,
        "observed_payer_country": (case.paypal_evidence or {}).get("payer_country"),
        "paypal": decomposition["paypal"],
        "library": {
            "base": decomposition["library_base"],
            "surcharge": decomposition["library_surcharge"],
            "decomposition": decomposition["library"],
        },
        "expected_fee": decomposition["library"]["final_fee"],
        "observed_fee": decomposition["paypal"]["fee"],
        "absolute_delta": str(delta),
        "delta_minor_units": minor_units(delta, case.currency),
        "effective_percentage_after_fixed": str(
            (((fee_d - fixed_d) / amount_d) * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount_d
            else None
        ),
        "formula": formula,
        "root_cause": root_cause,
        "account_configuration": account_config,
        "observations": observations,
        "product_id": case.product_id,
        "variant_id": case.variant_id,
        "payer_region": decomposition.get("payer_region"),
        "base_rule_id": decomposition.get("base_rule_id"),
        "fixed_fee_schedule_id": decomposition.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": decomposition.get("international_surcharge_schedule_id"),
        "paypal_data_revision": decomposition.get("data_revision"),
        "paypal_crawler_revision": decomposition.get("crawler_revision"),
    }

    diag_json = output_dir / "diagnostic.json"
    diag_json.write_text(json.dumps(diagnostic, indent=2, sort_keys=True))

    formula_json = output_dir / "formula-candidates.json"
    formula_json.write_text(json.dumps(formula, indent=2, sort_keys=True))

    md = _render_markdown(diagnostic)
    md_path = output_dir / "diagnostic.md"
    md_path.write_text(md)

    return {"diagnostic_json": diag_json, "diagnostic_md": md_path, "formula_candidates_json": formula_json}


def _render_markdown_table(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def _render_markdown(diag: dict[str, Any]) -> str:
    lines = [
        "# PayPal Sandbox Diagnostic Report",
        "",
        f"* Run ID: `{diag['run_id']}`",
        f"* Case ID: `{diag['case_id']}`",
        f"* Merchant country: `{diag['merchant_country']}`",
        f"* Configured buyer country: `{diag['buyer_country']}`",
        f"* Observed payer country: `{diag['observed_payer_country']}`",
        f"* Amount: `{diag['amount']} {diag['currency']}`",
        "",
        "## PayPal evidence",
        "",
        f"* Gross: `{diag['paypal']['gross']}`",
        f"* Fee: `{diag['paypal']['fee']}`",
        f"* Net: `{diag['paypal']['net']}`",
        "",
        "## Library decomposition",
        "",
        "### Base",
        "",
        _render_markdown_table([diag["library"]["base"]]),
        "",
        "### Surcharge",
        "",
        _render_markdown_table([diag["library"]["surcharge"]]),
        "",
        "### Totals",
        "",
        _render_markdown_table([diag["library"]["decomposition"]]),
        "",
        "## Comparison",
        "",
        f"* Expected library fee: `{diag['expected_fee']}`",
        f"* Observed PayPal fee: `{diag['observed_fee']}`",
        f"* Absolute delta: `{diag['absolute_delta']}`",
        f"* Delta minor units: `{diag['delta_minor_units']}`",
        f"* Implied effective percentage after fixed fee: `{diag['effective_percentage_after_fixed']}`",
        "",
        "## Formula inference",
        "",
        f"Stable linear formula found: `{diag['formula']['stable_linear_formula_found']}`",
        "",
    ]
    best = diag["formula"].get("best")
    if best:
        lines.extend(
            [
                "### Best candidate",
                "",
                _render_markdown_table([best]),
                "",
                "### Predictions",
                "",
                _render_markdown_table(best.get("predictions", [])),
                "",
            ]
        )
    lines.extend(
        [
            "## Root cause",
            "",
            f"* Category: `{diag['root_cause']['category']}`",
            f"* Confidence: `{diag['root_cause']['confidence']}`",
            f"* Explanation: {diag['root_cause']['explanation']}",
            "",
        ]
    )
    if diag.get("account_configuration"):
        lines.extend(
            [
                "## Account configuration",
                "",
                _render_markdown_table([diag["account_configuration"]]),
                "",
            ]
        )
    if diag.get("observations"):
        lines.extend(
            [
                "## Diagnostic observations",
                "",
                _render_markdown_table(diag["observations"]),
                "",
            ]
        )
    return "\n".join(lines)


def classify_de_checkout_outcome(
    association_verified: bool,
    manual_send_money: dict[str, Any],
    manual_order: dict[str, Any],
    playwright_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify the DE checkout failure layer from controlled path evidence.

    Paths:
    * A: manual Send Money (DE Personal -> DE Business)
    * B: Orders v2 manual browser approval
    * C: Orders v2 Playwright approval
    """
    if not association_verified:
        return {
            "status": "rest_credentials_merchant_mismatch",
            "reason": "REST order payee does not match the configured DE Business account.",
        }

    playwright_issues = {r.get("issue") for r in playwright_results if r.get("status") != "approved"}
    variants_tested = {r.get("payload_variant", "application_context") for r in playwright_results}
    variants_failed = {
        r.get("payload_variant", "application_context") for r in playwright_results if r.get("status") != "approved"
    }
    all_variants_failed = variants_tested and variants_tested == variants_failed
    some_variant_passed = any(r.get("status") == "approved" for r in playwright_results)

    # If one payload variant works and the other does not, the payload form is the differentiator.
    if len(playwright_results) > 1 and some_variant_passed and not all_variants_failed:
        return {
            "status": "orders_v2_payload_defect",
            "reason": "One Orders v2 payload variant succeeded while another failed.",
        }

    manual_order_status = manual_order.get("status")
    playwright_approved = any(r.get("status") == "approved" for r in playwright_results)

    if manual_order_status == "approved" and playwright_approved:
        return {
            "status": "transient_sandbox_error",
            "reason": "Manual and Playwright Orders v2 approvals both succeeded; previous failure was transient.",
        }

    if manual_order_status == "approved" and not playwright_approved:
        return {
            "status": "playwright_automation_defect",
            "reason": "Manual Orders v2 approval succeeded, but Playwright approval failed.",
        }

    send_money_succeeded = manual_send_money.get("succeeded", False)
    send_money_type = manual_send_money.get("transaction_type", "unknown")

    # If both Orders v2 paths fail and at least one shows a structured PayPal
    # compliance issue, the failure is at PayPal's checkout layer.
    compliance_issue = "COMPLIANCE_VIOLATION" in playwright_issues

    if all_variants_failed or manual_order_status in {"failed", "timeout"}:
        if send_money_succeeded and send_money_type != "friends/family" and compliance_issue:
            return {
                "status": "sandbox_checkout_limitation",
                "reason": "Send Money works, but Orders v2 checkout is blocked by a PayPal compliance restriction.",
            }
        if compliance_issue:
            return {
                "status": "account_configuration_difference",
                "reason": "PayPal returned a compliance violation on Orders v2 checkout.",
            }
        return {
            "status": "sandbox_checkout_limitation",
            "reason": "Orders v2 checkout failed and no payload or automation layer explains it.",
        }

    return {
        "status": "under_investigation",
        "reason": "Insufficient evidence to finalize DE checkout classification.",
    }


# expose alias
render_markdown = _render_markdown
