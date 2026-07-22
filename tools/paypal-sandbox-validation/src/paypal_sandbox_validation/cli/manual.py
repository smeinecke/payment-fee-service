from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.manual_flow import (
    build_manual_plan,
    infer_formula,
    run_manual_plan,
)
from paypal_sandbox_validation.models import (
    Case,
)
from paypal_sandbox_validation.persistence import (
    load_manual_results,
    manual_run_dir,
    save_manual_plan,
)
from paypal_sandbox_validation.planner import (
    generate_run_id,
)
from paypal_sandbox_validation.qualification import (
    default_qualification_path,
    is_diagnostic_sandbox_pricing_merchant,
    load_qualification_registry,
    save_qualification_registry,
)
from paypal_sandbox_validation.quote_adapter import minor_units, quantize_currency

from . import _env_csv_default, cli

_MANUAL_PROFILE_CASES: dict[str, list[tuple[str, str, str, str]]] = {
    "manual-de-first": [
        ("DE", "DE", "1.00", "EUR"),
    ],
    "manual-de-smoke": [
        ("DE", "DE", "1.00", "EUR"),
        ("DE", "DE", "10.00", "EUR"),
    ],
    "manual-de-formula": [
        ("DE", "DE", "1.00", "EUR"),
        ("DE", "DE", "10.00", "EUR"),
        ("DE", "DE", "100.00", "EUR"),
    ],
}


def _manual_run_id_option(run_id: str | None, profile: str) -> str:
    if run_id:
        return run_id
    return f"{profile}-{generate_run_id()}"


@cli.command("manual-plan")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
    help="Path to the PayPal Sandbox accounts CSV/TSV.",
)
@click.option(
    "--profile",
    default="manual-de-smoke",
    type=click.Choice(list(_MANUAL_PROFILE_CASES)),
    help="Manual validation profile.",
)
@click.option("--run-id", default=None, help="Run identifier (generated if omitted).")
def manual_plan(accounts_csv: str, profile: str, run_id: str | None) -> None:
    """Create a manual Send Money validation plan."""
    run_id = _manual_run_id_option(run_id, profile)
    cases = _MANUAL_PROFILE_CASES[profile]
    plan = build_manual_plan(run_id, profile, accounts_csv, cases)

    registry = load_qualification_registry()
    for case in plan:
        entry = registry.get(case.merchant_country, {})
        case.planning_time_registry_status = entry.get("status")
        if is_diagnostic_sandbox_pricing_merchant(entry):
            case.execution_classification = "diagnostic_sandbox_pricing"
        else:
            case.execution_classification = "public_rate_validation"

    save_manual_plan(run_id, plan)
    click.echo(f"Manual plan saved to {manual_run_dir(run_id)}")
    click.echo(f"Cases: {len(plan)}")
    for case in plan:
        fee = (case.quote or {}).get("processing_fee", {}).get("value") if case.quote else None
        click.echo(
            f"  {case.case_id}: {case.buyer_country}->{case.merchant_country} "
            f"{case.amount} {case.currency} (predicted fee {fee})"
        )


@cli.command("manual-run")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
    help="Path to the PayPal Sandbox accounts CSV/TSV.",
)
@click.option("--run-id", required=True, help="Run identifier.")
@click.option(
    "--continue-after-mismatch",
    is_flag=True,
    help="Continue through fee mismatches instead of stopping at the first.",
)
@click.option(
    "--headful",
    "headful_flag",
    is_flag=True,
    help="Run Playwright in headful mode.",
)
@click.option(
    "--headed",
    "headed_flag",
    is_flag=True,
    help="Alias for --headful.",
)
@click.option("--slow-mo", default=0, type=int, help="Playwright slow_mo delay in ms.")
def manual_run(
    accounts_csv: str,
    run_id: str,
    continue_after_mismatch: bool,
    headful_flag: bool,
    headed_flag: bool,
    slow_mo: int,
) -> None:
    """Execute a manual Send Money validation plan."""
    headless = not (headful_flag or headed_flag)
    results = run_manual_plan(
        run_id=run_id,
        accounts_csv=accounts_csv,
        stop_after_first_mismatch=not continue_after_mismatch,
        headless=headless,
        slow_mo=slow_mo,
    )
    click.echo(json.dumps(results, indent=2))
    _emit_manual_summary(run_id, results)


@cli.command("manual-report")
@click.option("--run-id", required=True, help="Run identifier.")
def manual_report(run_id: str) -> None:
    """Print a secret-free manual run report."""
    results = load_manual_results(run_id)
    _emit_manual_summary(run_id, results)


@cli.command("manual-qualify")
@click.option("--run-id", required=True, help="Run identifier.")
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def manual_qualify(run_id: str, qualification_registry: str) -> None:
    """Classify manual-send pricing from a run and update the qualification registry."""

    from paypal_sandbox_validation.qualification import (
        classify_manual_send_pricing,
        load_qualification_registry,
        update_manual_send_qualification,
    )

    results = load_manual_results(run_id)
    cases = [Case.model_validate(c) for c in results.get("cases", [])]
    classification = classify_manual_send_pricing(cases)
    if not classification.get("merchant_country"):
        click.echo("No classifiable manual-send observation found.", err=True)
        sys.exit(1)

    merchant_country = classification["merchant_country"]
    registry = load_qualification_registry(Path(qualification_registry))
    update_manual_send_qualification(registry, merchant_country, classification)
    save_qualification_registry(registry, Path(qualification_registry))
    click.echo(json.dumps(registry[merchant_country], indent=2))


def _emit_manual_summary(run_id: str, results: dict[str, Any]) -> None:
    cases = results.get("cases", [])
    total = len(cases)
    reconciled = sum(1 for c in cases if c.get("status") == "reconciled")
    failed = sum(1 for c in cases if c.get("status") == "failed")
    pending = total - reconciled - failed

    report: dict[str, Any] = {
        "run_id": run_id,
        "total_cases": total,
        "reconciled": reconciled,
        "failed": failed,
        "pending": pending,
        "cases": [],
    }

    for c in cases:
        evidence = c.get("paypal_evidence") or {}
        quote = c.get("quote") or {}
        reconciliation = c.get("reconciliation") or {}
        case_report = {
            "case_id": c.get("case_id"),
            "status": c.get("status"),
            "amount": c.get("amount"),
            "currency": c.get("currency"),
            "execution_path": c.get("execution_path"),
            "evidence_source": c.get("evidence_source"),
            "observed_transaction_type": evidence.get("transaction_type"),
            "observed_payment_type": evidence.get("payment_type"),
            "observed_payer_country": evidence.get("payer_country"),
            "paypal_gross": evidence.get("gross_amount", {}).get("value"),
            "paypal_fee": evidence.get("paypal_fee", {}).get("value"),
            "paypal_net": evidence.get("net_amount", {}).get("value"),
            "selected_product_id": c.get("product_id"),
            "selected_variant_id": c.get("variant_id"),
            "library_fee": quote.get("processing_fee", {}).get("value"),
            "library_net": quote.get("net_amount", {}).get("value"),
            "delta_minor_units": reconciliation.get("delta_minor_units"),
            "reconciliation_status": reconciliation.get("status"),
            "duplicate_prevention": (c.get("pilot_metadata") or {}).get("duplicate_prevention"),
            "product_selection_source": c.get("product_selection_source"),
            "prediction_provenance": c.get("prediction_provenance"),
            "prediction_created_before_original_submission": c.get("prediction_created_before_original_submission"),
            "prediction_created_before_observation_reuse": c.get("prediction_created_before_observation_reuse"),
            "original_submission_timestamp_known": c.get("original_submission_timestamp_known"),
            "prediction_sha256": c.get("prediction_sha256"),
            "prediction_unchanged_after_observation": c.get("prediction_unchanged_after_observation"),
        }
        report["cases"].append(case_report)

    # Infer the observed formula from fresh pre-submission predictions only.
    try:
        case_models = [Case.model_validate(c) for c in cases]
    except Exception:
        case_models = []
    fresh_cases = [c for c in case_models if c.prediction_provenance == "pre_submission_prediction"]
    formula = infer_formula(fresh_cases)
    if formula:
        historical_cases = [c for c in case_models if c.prediction_provenance == "historical_observation_requoted"]
        report["formula"] = formula
        report["formula"]["fresh_observations"] = [
            {"amount": o["amount"], "paypal_fee": o["paypal_fee"], "library_fee": o["library_fee"]}
            for o in formula["inferred_from_observations"]["observations"]
        ]
        report["formula"]["consistency_checks"] = _manual_consistency_checks(historical_cases, formula)

    click.echo(json.dumps(report, indent=2))


def _manual_consistency_checks(
    historical_cases: list[Case],
    formula: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check historical observations against the formula inferred from fresh ones."""
    checks: list[dict[str, Any]] = []
    inferred = formula.get("inferred_from_observations") or {}
    try:
        slope = Decimal(inferred["base_percentage"])
        intercept = Decimal(inferred["fixed_amount"])
    except Exception:
        return checks

    for case in historical_cases:
        ev = case.paypal_evidence or {}
        gross = ev.get("gross_amount", {}).get("value")
        observed = ev.get("paypal_fee", {}).get("value")
        if gross is None or observed is None:
            continue
        currency = case.currency
        expected = (Decimal(gross) * slope + intercept).quantize(Decimal("0.0001"))
        rounded = quantize_currency(expected, currency)
        checks.append(
            {
                "case_id": case.case_id,
                "amount": str(gross),
                "observed_paypal_fee": str(observed),
                "expected_paypal_fee": str(rounded),
                "raw_expected": str(expected),
                "matches": minor_units(rounded, currency) == minor_units(observed, currency),
                "note": "historical supporting observation",
            }
        )
    return checks
