from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.approval import approve_order
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.capture import extract_capture_evidence
from paypal_sandbox_validation.configuration import (
    load_scenarios,
    validate_configuration,
)
from paypal_sandbox_validation.models import (
    Account,
    Case,
    CaseStatus,
    ReconciliationStatus,
    RunConfig,
)
from paypal_sandbox_validation.oauth import OAuthCache, fetch_token, probe_credentials
from paypal_sandbox_validation.paypal_api import (
    PayPalAPIError,
    PayPalClient,
    build_order_payload,
    extract_approval_url,
)
from paypal_sandbox_validation.persistence import (
    artifact_root,
    load_plan,
    load_results,
    save_case,
    save_configuration_summary,
    save_json,
    save_plan,
    save_results,
    save_sanitized_capture,
    save_sanitized_order,
)
from paypal_sandbox_validation.planner import (
    build_plan,
    enrich_plan_with_products,
    generate_request_id,
    generate_run_id,
    plan_summary,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter, QuoteResolutionError
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.redaction import redact_path, sanitize_dict
from paypal_sandbox_validation.reporting import build_summary, save_junit, save_summary, save_summary_markdown


def _env_csv_default() -> str | None:
    return os.environ.get("PAYPAL_SANDBOX_ACCOUNTS_CSV")


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """PayPal Sandbox fee-reconciliation harness."""


@cli.command("validate-config")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
    help="Path to the PayPal Sandbox accounts CSV/TSV.",
)
def validate_config_cmd(accounts_csv: str) -> None:
    """Validate the account CSV and workspace configuration."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts)
    config_check = validate_configuration(accounts_csv, accounts)

    summary = {
        "merchants_valid": validation["merchant_count"],
        "buyers_valid": validation["buyer_count"],
        "rest_credential_pairs": sum(1 for a in accounts if a.is_business() and a.client_id and a.secret),
        "duplicate_accounts": len(validation["duplicate_accounts"]),
        "duplicate_client_ids": len(validation["duplicate_client_ids"]),
        "invalid_business_credentials": len(validation["invalid_business_credentials"]),
        "missing_fields": len(validation["missing_business_credentials"])
        + len(validation["missing_personal_credentials"]),
        "live_endpoints": len(config_check["live_hosts_found"]),
        "csv_tracked": config_check["csv_tracked"],
        "gitignore_complete": config_check["gitignore_complete"],
        "ready_for_probe": validation["valid"]
        and config_check["gitignore_complete"]
        and not config_check["csv_tracked"],
    }

    for key, value in summary.items():
        click.echo(f"{key} = {value}")

    if not summary["ready_for_probe"]:
        click.echo("Configuration validation failed.", err=True)
        sys.exit(1)


@cli.command("probe")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
def probe_cmd(accounts_csv: str) -> None:
    """Probe OAuth credentials for every Business merchant."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts)

    invalid_countries = set(validation["invalid_business_credentials"])
    if invalid_countries:
        click.echo(
            f"Skipping {len(invalid_countries)} business accounts with invalid credentials (client_id == secret).",
            err=True,
        )

    results: list[dict] = []
    for account in accounts:
        if not account.is_business():
            continue
        if account.country_code in invalid_countries:
            continue
        assert account.client_id and account.secret
        result = probe_credentials(account.client_id, account.secret, account.country_code)
        results.append(
            {
                "country": result.country,
                "status": result.status.value,
                "expires_in": result.expires_in,
                "scope_count": result.scope_count,
                "classification": result.classification,
            }
        )
        click.echo(f"{result.country}: {result.status.value}")

    failures = [r for r in results if r["status"] != "success"]
    if failures:
        click.echo(f"Probe failures: {len(failures)}", err=True)
        sys.exit(1)


@cli.command("plan")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--profile", type=click.Choice(["smoke", "de-pilot", "full"], case_sensitive=False), default="smoke")
@click.option("--merchant", type=str, default=None)
@click.option("--buyer", type=str, default=None)
@click.option("--case-id", type=str, default=None)
@click.option("--amount", type=str, default=None)
@click.option("--currency", type=str, default=None)
@click.option("--max-cases", type=int, default=None)
@click.option("--confirm-full-matrix", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
def plan_cmd(
    accounts_csv: str,
    profile: str,
    merchant: str | None,
    buyer: str | None,
    case_id: str | None,
    amount: str | None,
    currency: str | None,
    max_cases: int | None,
    confirm_full_matrix: bool,
    dry_run: bool,
) -> None:
    """Generate and persist a validation plan."""
    scenarios = load_scenarios()
    run_id = generate_run_id()
    plan = build_plan(
        run_id=run_id,
        profile_name=profile,
        scenarios=scenarios,
        merchant_filter=merchant,
        buyer_filter=buyer,
        amount_override=amount,
        currency_override=currency,
        max_cases=max_cases,
        confirm_full_matrix=confirm_full_matrix,
    )
    adapter = QuoteAdapter()
    plan = enrich_plan_with_products(plan, adapter)
    save_plan(run_id, plan)
    summary = plan_summary(plan)
    click.echo(json.dumps(summary, indent=2))

    config = RunConfig(
        accounts_csv=accounts_csv,
        profile=profile,
        merchant=merchant,
        buyer=buyer,
        case_id=case_id,
        amount=amount,
        currency=currency,
        max_cases=max_cases,
        dry_run=dry_run,
        confirm_full_matrix=confirm_full_matrix,
    )
    save_json(Path("artifacts/paypal-sandbox") / run_id / "run-config.json", config.model_dump())


@cli.command("run")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--profile", type=click.Choice(["smoke", "de-pilot", "full"], case_sensitive=False), default="smoke")
@click.option("--merchant", type=str, default=None)
@click.option("--buyer", type=str, default=None)
@click.option("--case-id", type=str, default=None)
@click.option("--amount", type=str, default=None)
@click.option("--currency", type=str, default=None)
@click.option("--headful", is_flag=True, default=False)
@click.option("--headed", is_flag=True, default=False)
@click.option("--slow-mo", type=int, default=0)
@click.option("--max-cases", type=int, default=None)
@click.option("--resume", type=str, default=None)
@click.option("--continue-after-mismatch", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--confirm-full-matrix", is_flag=True, default=False)
def run_cmd(
    accounts_csv: str,
    profile: str,
    merchant: str | None,
    buyer: str | None,
    case_id: str | None,
    amount: str | None,
    currency: str | None,
    headful: bool,
    headed: bool,
    slow_mo: int,
    max_cases: int | None,
    resume: str | None,
    continue_after_mismatch: bool,
    dry_run: bool,
    confirm_full_matrix: bool,
) -> None:
    """Execute the validation plan against PayPal Sandbox."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts)

    invalid_countries = set(validation["invalid_business_credentials"])
    if invalid_countries:
        click.echo(
            f"Warning: {len(invalid_countries)} business accounts have invalid "
            "credentials (client_id == secret) and will be skipped.",
            err=True,
        )

    merchant_accounts = {
        a.country_code: a
        for a in accounts
        if a.is_business() and a.client_id and a.secret and a.country_code not in invalid_countries
    }
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal() and a.password}

    scenarios = load_scenarios()
    run_id = resume or generate_run_id()
    config = RunConfig(
        accounts_csv=accounts_csv,
        profile=profile,
        merchant=merchant,
        buyer=buyer,
        case_id=case_id,
        amount=amount,
        currency=currency,
        headful=headful or headed,
        headed=headful or headed,
        slow_mo=slow_mo,
        max_cases=max_cases,
        resume=resume,
        continue_after_mismatch=continue_after_mismatch,
        dry_run=dry_run,
        confirm_full_matrix=confirm_full_matrix,
    )

    if resume:
        plan = load_plan(run_id)
    else:
        plan = build_plan(
            run_id=run_id,
            profile_name=profile,
            scenarios=scenarios,
            merchant_filter=merchant,
            buyer_filter=buyer,
            amount_override=amount,
            currency_override=currency,
            max_cases=max_cases,
            confirm_full_matrix=confirm_full_matrix,
        )
        adapter = QuoteAdapter()
        plan = enrich_plan_with_products(plan, adapter)
        save_plan(run_id, plan)

    save_json(
        Path("artifacts/paypal-sandbox") / run_id / "run-config.json",
        sanitize_dict(config.model_dump()),
    )

    save_configuration_summary(
        run_id,
        {
            "csv_path": redact_path(accounts_csv),
            "merchant_count": len(merchant_accounts),
            "buyer_count": len(buyer_accounts),
            "profile": profile,
            "run_id": run_id,
        },
    )

    adapter = QuoteAdapter()
    oauth_cache = OAuthCache()
    results: dict[str, Any] = {"run_id": run_id, "cases": []}

    for case in plan:
        if case_id and case.case_id != case_id:
            continue
        result = _run_case(
            case=case,
            config=config,
            merchant_accounts=merchant_accounts,
            buyer_accounts=buyer_accounts,
            adapter=adapter,
            oauth_cache=oauth_cache,
        )
        results["cases"].append(result)
        save_results(run_id, results)

        rec = result.get("reconciliation", {}) or {}
        if rec.get("status") == "fee_mismatch" and not continue_after_mismatch:
            click.echo(f"Stopping after first fee mismatch: {case.case_id}")
            break

    summary = build_summary(run_id)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary, accounts_csv)
    save_junit(run_id, summary)
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))


def _run_case(
    case: Case,
    config: RunConfig,
    merchant_accounts: dict[str, Account],
    buyer_accounts: dict[str, Account],
    adapter: QuoteAdapter,
    oauth_cache: OAuthCache,
) -> dict[str, Any]:
    merchant = merchant_accounts.get(case.merchant_country)
    buyer = buyer_accounts.get(case.buyer_country)
    if not merchant or not buyer:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error="Missing merchant or buyer account")

    case.request_id_create = generate_request_id(case.run_id, case.case_id, "create", 0)

    # 1. Library prediction before creating any PayPal transaction.
    try:
        quote = adapter.build_quote(
            merchant.country_code,
            buyer.country_code,
            case.amount,
            case.currency,
        )
        case.quote = quote
        case.product_id = quote["_scenario"]["product_id"]
        case.variant_id = quote["_scenario"]["variant_id"]
    except QuoteResolutionError as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=str(exc), reconciliation_status=exc.status)
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Library quote failed: {exc}")

    if config.dry_run:
        case.status = CaseStatus.PREDICTION_READY
        return _case_dict(case)

    # 2. OAuth token for merchant.
    try:
        assert merchant.client_id and merchant.secret
        token = fetch_token(oauth_cache, merchant.client_id, merchant.secret, merchant.country_code)
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"OAuth failed: {exc}")

    client = PayPalClient(token=token)

    # 3. Callback server and order creation.
    invoice_id = f"{case.run_id}-{case.case_id}"
    callback = CallbackServer(expected_token="")
    try:
        payload = build_order_payload(
            amount=case.amount,
            currency=case.currency,
            return_url=callback.return_url,
            cancel_url=callback.cancel_url,
            reference_id=case.case_id,
            invoice_id=invoice_id,
            custom_id=case.case_id,
        )
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Failed to build order payload: {exc}")

    callback.start()
    try:
        order = client.create_order(payload, request_id=case.request_id_create)
        case.order_id = order.get("id")
        case.status = CaseStatus.ORDER_CREATED
        save_sanitized_order(case.run_id, case.case_id, order)

        if not case.order_id:
            return _case_dict(case, error="Order response missing id")

        callback.update_expected_token(case.order_id)
        approval_url = extract_approval_url(order)
        case.approval_url = approval_url

        # 4. Buyer approval via Playwright.
        screenshot_dir = Path("artifacts/paypal-sandbox") / case.run_id / "screenshots"
        approval_result = approve_order(
            buyer=buyer,
            approval_url=approval_url,
            amount=case.amount,
            currency=case.currency,
            order_token=case.order_id,
            headless=not (config.headful or config.headed),
            slow_mo=config.slow_mo,
            screenshot_dir=screenshot_dir,
            case_id=case.case_id,
            callback_server=callback,
        )
    except PayPalAPIError as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Order creation failed: {exc}")
    finally:
        callback.stop()
    if approval_result["status"] != "approved":
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=approval_result.get("error"), reconciliation_status=approval_result["status"])
    case.status = CaseStatus.BUYER_APPROVED

    # 5. Capture.
    case.request_id_capture = generate_request_id(case.run_id, case.case_id, "capture", 0)
    try:
        capture_response = client.capture_order(case.order_id, request_id=case.request_id_capture)
    except PayPalAPIError as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Capture failed: {exc}")

    try:
        evidence = extract_capture_evidence(capture_response)
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Capture evidence extraction failed: {exc}")

    case.capture_id = evidence.get("capture_id")
    case.paypal_evidence = evidence
    case.status = CaseStatus.CAPTURED
    save_sanitized_capture(case.run_id, case.case_id, capture_response)

    # 6. Reconciliation.
    observed_payer_country = evidence.get("payer_country")
    if observed_payer_country and observed_payer_country != buyer.country_code:
        rec_result = reconcile(evidence, case.quote, merchant.country_code, buyer.country_code, observed_payer_country)
        rec_result.status = ReconciliationStatus.BUYER_COUNTRY_MISMATCH
    else:
        rec_result = reconcile(evidence, case.quote, merchant.country_code, buyer.country_code, observed_payer_country)
    case.reconciliation = rec_result.model_dump()
    case.status = CaseStatus.RECONCILED
    save_case(case.run_id, case)
    return _case_dict(case)


def _case_dict(case: Case, error: str | None = None, reconciliation_status: str | None = None) -> dict[str, Any]:
    data = case.model_dump()
    if error:
        data["error"] = error
    if reconciliation_status:
        rec = data.get("reconciliation") or {}
        rec["status"] = reconciliation_status
        data["reconciliation"] = rec
    return data


@cli.command("reconcile")
@click.option("--run-id", type=str, required=True)
def reconcile_cmd(run_id: str) -> None:
    """Re-run reconciliation for a captured run without creating new orders."""
    cases = []
    for case in load_results(run_id).get("cases", []):
        if case.get("status") == "captured" and case.get("paypal_evidence") and case.get("quote"):
            rec = reconcile(
                case["paypal_evidence"],
                case["quote"],
                case["merchant_country"],
                case["buyer_country"],
                case["paypal_evidence"].get("payer_country"),
            )
            case["reconciliation"] = rec.model_dump()
            case["status"] = "reconciled"
        cases.append(case)
    save_results(run_id, {"run_id": run_id, "cases": cases})
    summary = build_summary(run_id)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary)
    save_junit(run_id, summary)
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))


@cli.command("report")
@click.option("--run-id", type=str, required=True)
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
def report_cmd(run_id: str, accounts_csv: str) -> None:
    """Generate a sanitized report for a run."""
    summary = build_summary(run_id)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary, accounts_csv)
    save_junit(run_id, summary)

    click.echo("Sanitized report generated.")
    click.echo(f"Artifact path: {artifact_root() / run_id}")
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
