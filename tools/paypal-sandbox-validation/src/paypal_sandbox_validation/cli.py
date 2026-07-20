from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.approval import approve_order
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.configuration import (
    load_scenarios,
    validate_configuration,
)
from paypal_sandbox_validation.manual_flow import (
    build_manual_plan,
    infer_formula,
    run_manual_plan,
)
from paypal_sandbox_validation.models import (
    Account,
    Case,
    CaseStatus,
    ReconciliationStatus,
    RunConfig,
)
from paypal_sandbox_validation.nvp import PayPalNVPClient
from paypal_sandbox_validation.oauth import OAuthCache, OAuthError, OAuthProbeStatus, fetch_token, probe_credentials
from paypal_sandbox_validation.paypal_api import (
    PayPalAPIError,
    PayPalClient,
    build_order_payload,
    extract_approval_url,
    extract_payee_info,
    extract_paypal_error_fields,
    order_payload_signature,
)
from paypal_sandbox_validation.persistence import (
    artifact_root,
    load_manual_results,
    load_plan,
    load_results,
    manual_run_dir,
    save_case,
    save_configuration_summary,
    save_json,
    save_manual_plan,
    save_plan,
    save_results,
    save_sanitized_order,
)
from paypal_sandbox_validation.planner import (
    build_plan,
    build_regional_pilot_plan,
    build_surcharge_pilot_plan,
    enrich_plan_with_products,
    ensure_surcharge_case,
    generate_request_id,
    generate_run_id,
    plan_summary,
)
from paypal_sandbox_validation.qualification import (
    build_qualification_plan,
    build_validation_plan,
    classify_qualification,
    default_qualification_path,
    is_merchant_excluded,
    load_qualification_registry,
    promote_observation_fixtures,
    qualification_summary,
    save_qualification_registry,
    save_qualification_report,
    save_validation_report,
    validation_summary,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter, QuoteResolutionError
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.redaction import mask_value, redact_path, sanitize_dict, sanitize_paypal_order
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
        "merchants_present": validation["merchant_count"],
        "merchants_valid": validation["merchants_valid"],
        "buyers_present": validation["buyer_count"],
        "buyers_valid": validation["buyers_valid"],
        "rest_credential_pairs": sum(1 for a in accounts if a.is_business() and a.client_id and a.secret),
        "nvp_credential_triples": sum(
            1 for a in accounts if a.is_business() and a.nvp_user and a.nvp_password and a.nvp_signature
        ),
        "duplicate_accounts": len(validation["duplicate_accounts"]),
        "duplicate_client_ids": len(validation["duplicate_client_ids"]),
        "duplicate_nvp_users": len(validation["duplicate_nvp_users"]),
        "invalid_business_credentials": len(validation["invalid_business_credentials"]),
        "missing_business_credentials": len(validation["missing_business_credentials"]),
        "missing_nvp_credentials": len(validation["missing_nvp_credentials"]),
        "incomplete_nvp_credentials": len(validation["incomplete_nvp_credentials"]),
        "missing_personal_credentials": len(validation["missing_personal_credentials"]),
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

    merchants = [a for a in accounts if a.is_business()]
    summary = {
        "probe_run_id": generate_run_id(),
        "merchants_present": len(merchants),
        "merchants_valid": validation["merchants_valid"],
        "merchants_probed": 0,
        "oauth_successful": 0,
        "oauth_failed": 0,
        "oauth_skipped": 0,
    }

    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot probe.", err=True)
        for key, value in summary.items():
            click.echo(f"{key} = {value}")
        sys.exit(1)

    results: list[dict] = []
    for account in merchants:
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

    summary["merchants_probed"] = len(merchants)
    summary["oauth_successful"] = sum(1 for r in results if r["status"] == "success")
    summary["oauth_failed"] = sum(1 for r in results if r["status"] != "success")
    summary["oauth_skipped"] = 0

    from paypal_sandbox_validation.persistence import save_oauth_probe_summary

    save_oauth_probe_summary(summary["probe_run_id"], results)
    summary_path = Path("artifacts/paypal-sandbox") / summary["probe_run_id"] / "oauth-probe-summary.json"
    save_json(summary_path, summary)

    for key, value in summary.items():
        click.echo(f"{key} = {value}")

    if summary["oauth_failed"] or summary["merchants_probed"] != summary["merchants_present"]:
        click.echo("Probe failed.", err=True)
        sys.exit(1)


@cli.command("probe-nvp")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
def probe_nvp_cmd(accounts_csv: str) -> None:
    """Probe NVP credentials for every Business merchant."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts)

    merchants = [a for a in accounts if a.is_business()]
    summary = {
        "probe_run_id": generate_run_id(),
        "merchants_selected": len(merchants),
        "nvp_credentials_valid": 0,
        "nvp_credentials_failed": 0,
        "nvp_credentials_skipped": 0,
    }

    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot probe NVP.", err=True)
        for key, value in summary.items():
            click.echo(f"{key} = {value}")
        sys.exit(1)

    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    for account in merchants:
        if not account.nvp_user or not account.nvp_password or not account.nvp_signature:
            summary["nvp_credentials_skipped"] += 1
            click.echo(f"{account.country_code}: skipped")
            continue
        try:
            with PayPalNVPClient(account) as client:
                response = client.transaction_search(start_date=start_iso, end_date=end_iso)
            if response.is_success():
                summary["nvp_credentials_valid"] += 1
                click.echo(f"{account.country_code}: valid")
            else:
                summary["nvp_credentials_failed"] += 1
                click.echo(f"{account.country_code}: failed")
        except Exception as exc:
            summary["nvp_credentials_failed"] += 1
            click.echo(f"{account.country_code}: failed ({type(exc).__name__})")

    summary_path = Path("artifacts/paypal-sandbox") / summary["probe_run_id"] / "nvp-probe-summary.json"
    save_json(summary_path, summary)

    for key, value in summary.items():
        click.echo(f"{key} = {value}")

    if (
        summary["nvp_credentials_failed"]
        or summary["nvp_credentials_skipped"]
        or summary["nvp_credentials_valid"] != summary["merchants_selected"]
    ):
        click.echo("NVP probe failed.", err=True)
        sys.exit(1)


@cli.command("plan")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option(
    "--profile",
    type=click.Choice(["smoke", "de-compliance-probe", "de-pilot", "full"], case_sensitive=False),
    default="smoke",
)
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
    if profile == "smoke":
        plan = ensure_surcharge_case(plan, adapter)
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
@click.option(
    "--profile",
    type=click.Choice(["smoke", "de-compliance-probe", "de-pilot", "full"], case_sensitive=False),
    default="smoke",
)
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
@click.option("--retry-failed", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--confirm-full-matrix", is_flag=True, default=False)
@click.option(
    "--payload-variant",
    type=click.Choice(["application_context", "payment_source"], case_sensitive=False),
    default="application_context",
)
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
    retry_failed: bool,
    dry_run: bool,
    confirm_full_matrix: bool,
    payload_variant: str,
) -> None:
    """Execute the validation plan against PayPal Sandbox."""
    accounts = parse_accounts_csv(accounts_csv)
    require_complete = confirm_full_matrix or profile == "full"
    validation = validate_accounts(accounts, require_complete=require_complete)

    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot run.", err=True)
        sys.exit(1)

    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}

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
        retry_failed=retry_failed,
        dry_run=dry_run,
        confirm_full_matrix=confirm_full_matrix,
        payload_variant=payload_variant,
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
        if profile == "smoke":
            plan = ensure_surcharge_case(plan, adapter)
        save_plan(run_id, plan)

    adapter = QuoteAdapter()
    oauth_cache = OAuthCache()
    output = _execute_plan(
        run_id=run_id,
        plan=plan,
        config=config,
        merchant_accounts=merchant_accounts,
        buyer_accounts=buyer_accounts,
        adapter=adapter,
        oauth_cache=oauth_cache,
        resume=bool(resume),
    )
    click.echo(json.dumps(output, indent=2))


REGIONAL_PILOT_MERCHANTS = [
    "US",
    "ES",
    "GB",
    "JP",
    "CA",
    "AU",
    "CH",
    "BR",
    "HK",
    "CZ",
    "IL",
    "ZA",
    "DE",
]


@cli.command("surcharge-pilot")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--merchant", type=str, default="US")
@click.option("--amount", type=str, default="10.00")
@click.option("--currency", type=str, default="USD")
@click.option("--headful", is_flag=True, default=False)
@click.option("--headed", is_flag=True, default=False)
@click.option("--slow-mo", type=int, default=0)
@click.option("--continue-after-mismatch", is_flag=True, default=False)
@click.option(
    "--buyers",
    type=str,
    default=",".join(["DE", "GB", "JP", "AU", "BR", "HK", "IL", "ZA"]),
)
@click.option("--resume", type=str, default=None)
def surcharge_pilot_cmd(
    accounts_csv: str,
    merchant: str,
    amount: str,
    currency: str,
    headful: bool,
    headed: bool,
    slow_mo: int,
    continue_after_mismatch: bool,
    buyers: str,
    resume: str | None,
) -> None:
    """Execute the US domestic control + nonzero-surcharge international pilot."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts, require_complete=False)
    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot run.", err=True)
        sys.exit(1)

    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    buyer_countries = {a.country_code for a in buyer_accounts.values()}
    candidate_buyers = [b.strip().upper() for b in buyers.split(",") if b.strip()]

    run_id = resume or generate_run_id()
    config = RunConfig(
        accounts_csv=accounts_csv,
        profile="surcharge-pilot",
        merchant=merchant,
        amount=amount,
        currency=currency,
        headful=headful or headed,
        headed=headful or headed,
        slow_mo=slow_mo,
        continue_after_mismatch=continue_after_mismatch,
        resume=resume,
    )

    adapter = QuoteAdapter()
    if resume:
        plan = load_plan(run_id)
        no_surcharge_candidate = False
    else:
        plan, found = build_surcharge_pilot_plan(
            run_id=run_id,
            merchant_country=merchant,
            buyer_countries=buyer_countries,
            adapter=adapter,
            amount=amount,
            currency=currency,
            candidate_buyers=candidate_buyers,
        )
        save_plan(run_id, plan)
        no_surcharge_candidate = not found

    oauth_cache = OAuthCache()
    output = _execute_plan(
        run_id=run_id,
        plan=plan,
        config=config,
        merchant_accounts=merchant_accounts,
        buyer_accounts=buyer_accounts,
        adapter=adapter,
        oauth_cache=oauth_cache,
        resume=bool(resume),
        extra_summary={
            "no_surcharge_candidate": no_surcharge_candidate,
            "merchant": merchant,
            "amount": amount,
            "currency": currency,
        },
    )
    click.echo(json.dumps(output, indent=2))


@cli.command("regional-pilot")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--max-cases", type=int, default=24)
@click.option("--headful", is_flag=True, default=False)
@click.option("--headed", is_flag=True, default=False)
@click.option("--slow-mo", type=int, default=0)
@click.option("--continue-after-mismatch", is_flag=True, default=False)
@click.option("--resume", type=str, default=None)
@click.option("--include-unsuitable", is_flag=True, default=False)
@click.option(
    "--diagnostic-sandbox-pricing",
    is_flag=True,
    default=False,
    help="Include merchants with sandbox-specific pricing (e.g. DE) as diagnostic cases.",
)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def regional_pilot_cmd(
    accounts_csv: str,
    max_cases: int,
    headful: bool,
    headed: bool,
    slow_mo: int,
    continue_after_mismatch: bool,
    resume: str | None,
    include_unsuitable: bool,
    diagnostic_sandbox_pricing: bool,
    qualification_registry: str,
) -> None:
    """Execute the small regional Merchant pilot (two cases per qualified merchant)."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts, require_complete=False)
    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot run.", err=True)
        sys.exit(1)

    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    configured_merchants = set(merchant_accounts.keys())
    registry = load_qualification_registry(Path(qualification_registry))

    override = include_unsuitable or diagnostic_sandbox_pricing

    def _allowed(m: str) -> bool:
        return m in configured_merchants and not is_merchant_excluded(m, registry, override=override)

    regional_merchants = [m for m in REGIONAL_PILOT_MERCHANTS if _allowed(m)]
    buyer_countries = {a.country_code for a in buyer_accounts.values()}

    run_id = resume or generate_run_id()
    config = RunConfig(
        accounts_csv=accounts_csv,
        profile="regional-pilot",
        headful=headful or headed,
        headed=headful or headed,
        slow_mo=slow_mo,
        continue_after_mismatch=continue_after_mismatch,
        max_cases=max_cases,
        resume=resume,
    )

    adapter = QuoteAdapter()
    if resume:
        plan = load_plan(run_id)
        pilot_summary: dict[str, Any] = {}
    else:
        plan, pilot_summary = build_regional_pilot_plan(
            run_id=run_id,
            merchant_countries=regional_merchants,
            buyer_countries=buyer_countries,
            adapter=adapter,
            max_cases=max_cases,
        )
        if diagnostic_sandbox_pricing:
            _mark_diagnostic_sandbox_pricing(plan, registry)
        save_plan(run_id, plan)

    oauth_cache = OAuthCache()
    output = _execute_plan(
        run_id=run_id,
        plan=plan,
        config=config,
        merchant_accounts=merchant_accounts,
        buyer_accounts=buyer_accounts,
        adapter=adapter,
        oauth_cache=oauth_cache,
        resume=bool(resume),
        extra_summary={
            "regional_pilot_plan_summary": pilot_summary,
            "qualification_registry": str(qualification_registry),
        },
    )
    click.echo(json.dumps(output, indent=2))


@cli.command("qualify")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option(
    "--merchants",
    type=str,
    default=None,
    help="Comma-separated merchant countries to qualify (default: all configured except known exclusions).",
)
@click.option("--max-cases-per-merchant", type=int, default=3)
@click.option("--headful", is_flag=True, default=False)
@click.option("--headed", is_flag=True, default=False)
@click.option("--slow-mo", type=int, default=0)
@click.option("--resume", type=str, default=None)
@click.option("--include-unsuitable", is_flag=True, default=False)
@click.option(
    "--diagnostic-sandbox-pricing",
    is_flag=True,
    default=False,
    help="Include merchants with sandbox-specific pricing (e.g. DE) for diagnostic qualification.",
)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def qualify_cmd(
    accounts_csv: str,
    merchants: str | None,
    max_cases_per_merchant: int,
    headful: bool,
    headed: bool,
    slow_mo: int,
    resume: str | None,
    include_unsuitable: bool,
    diagnostic_sandbox_pricing: bool,
    qualification_registry: str,
) -> None:
    """Run a bounded three-capture qualification per merchant."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts, require_complete=False)
    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot run.", err=True)
        sys.exit(1)

    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    buyer_countries = {a.country_code for a in buyer_accounts.values()}

    registry = load_qualification_registry(Path(qualification_registry))
    override = include_unsuitable or diagnostic_sandbox_pricing
    if merchants:
        target_merchants = [m.strip().upper() for m in merchants.split(",") if m.strip()]
    else:

        def _allowed_qual(m: str) -> bool:
            return m in merchant_accounts and not is_merchant_excluded(m, registry, override=override)

        target_merchants = [m for m in REGIONAL_PILOT_MERCHANTS if _allowed_qual(m)]

    adapter = QuoteAdapter()
    run_id = resume or generate_run_id()

    if resume:
        plan = load_plan(run_id)
    else:
        plan = build_qualification_plan(
            run_id=run_id,
            merchants=target_merchants,
            buyer_countries=buyer_countries,
            adapter=adapter,
            max_cases_per_merchant=max_cases_per_merchant,
        )
        save_plan(run_id, plan)

    config = RunConfig(
        accounts_csv=accounts_csv,
        profile="qualification",
        headful=headful or headed,
        headed=headful or headed,
        slow_mo=slow_mo,
    )
    oauth_cache = OAuthCache()

    first = not bool(resume)
    for merchant in target_merchants:
        merchant_plan = [c for c in plan if c.merchant_country == merchant]
        if not merchant_plan:
            continue
        config.resume = run_id if not first else None
        _execute_plan(
            run_id=run_id,
            plan=merchant_plan,
            config=config,
            merchant_accounts=merchant_accounts,
            buyer_accounts=buyer_accounts,
            adapter=adapter,
            oauth_cache=oauth_cache,
            resume=not first,
        )
        results = load_results(run_id)
        merchant_cases = [
            Case.model_validate({k: v for k, v in c.items() if k in Case.model_fields})
            for c in results["cases"]
            if c["merchant_country"] == merchant
        ]
        account_config = _merchant_account_config(accounts_csv, merchant)
        registry[merchant] = classify_qualification(merchant, merchant_cases, adapter, account_config)
        save_qualification_registry(registry, Path(qualification_registry))
        first = False

    results = load_results(run_id)
    all_cases = [Case.model_validate({k: v for k, v in c.items() if k in Case.model_fields}) for c in results["cases"]]
    attempted = set(target_merchants)
    report_paths = save_qualification_report(run_id, registry, all_cases, attempted_merchants=attempted)
    summary = qualification_summary(registry, attempted_merchants=attempted)
    click.echo("Qualification complete.")
    click.echo(f"Run ID: {run_id}")
    click.echo(f"Report: {report_paths['md']}")
    click.echo(json.dumps(summary, indent=2))


@cli.command("regional-validation")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--max-merchants", type=int, default=0)
@click.option("--headful", is_flag=True, default=False)
@click.option("--headed", is_flag=True, default=False)
@click.option("--slow-mo", type=int, default=0)
@click.option("--resume", type=str, default=None)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def regional_validation_cmd(
    accounts_csv: str,
    max_merchants: int,
    headful: bool,
    headed: bool,
    slow_mo: int,
    resume: str | None,
    qualification_registry: str,
) -> None:
    """Validate one domestic and one distinct/surcharge case per representative merchant."""
    accounts = parse_accounts_csv(accounts_csv)
    validation = validate_accounts(accounts, require_complete=False)
    if not validation["valid"]:
        click.echo("Account configuration is invalid; cannot run.", err=True)
        sys.exit(1)

    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    buyer_countries = {a.country_code for a in buyer_accounts.values()}

    registry = load_qualification_registry(Path(qualification_registry))
    representative = [
        m for m, q in sorted(registry.items()) if q.get("status") == "representative" and m in merchant_accounts
    ]
    if max_merchants:
        representative = representative[:max_merchants]

    adapter = QuoteAdapter()
    run_id = resume or generate_run_id()
    if resume:
        plan = load_plan(run_id)
    else:
        plan = build_validation_plan(
            run_id=run_id,
            qualified_merchants=representative,
            buyer_countries=buyer_countries,
            adapter=adapter,
        )
        save_plan(run_id, plan)

    config = RunConfig(
        accounts_csv=accounts_csv,
        profile="regional-validation",
        headful=headful or headed,
        headed=headful or headed,
        slow_mo=slow_mo,
        continue_after_mismatch=False,
    )
    oauth_cache = OAuthCache()
    first = not bool(resume)
    for merchant in representative:
        merchant_plan = [c for c in plan if c.merchant_country == merchant]
        if not merchant_plan:
            continue
        config.resume = run_id if not first else None
        _execute_plan(
            run_id=run_id,
            plan=merchant_plan,
            config=config,
            merchant_accounts=merchant_accounts,
            buyer_accounts=buyer_accounts,
            adapter=adapter,
            oauth_cache=oauth_cache,
            resume=not first,
        )
        first = False

    results = load_results(run_id)
    cases = [Case.model_validate({k: v for k, v in c.items() if k in Case.model_fields}) for c in results["cases"]]
    report_paths = save_validation_report(run_id, cases, registry)
    fixture_paths = promote_observation_fixtures(cases)
    summary = validation_summary(cases, registry)
    click.echo("Regional validation complete.")
    click.echo(f"Run ID: {run_id}")
    click.echo(f"Report: {report_paths['md']}")
    click.echo(f"Observation fixtures: {len(fixture_paths)}")
    click.echo(json.dumps(summary, indent=2))


def _merge_existing_case(case: Case, existing: dict[str, Any]) -> Case:
    """Hydrate a planned case with persisted progress so idempotency keys are reused."""
    allowed = set(Case.model_fields.keys())
    filtered_existing = {k: v for k, v in existing.items() if k in allowed}
    existing_case = Case.model_validate(filtered_existing)
    case.status = existing_case.status
    case.request_id_create = existing_case.request_id_create or case.request_id_create
    case.request_id_capture = existing_case.request_id_capture or case.request_id_capture
    case.create_attempts = existing_case.create_attempts or 0
    case.capture_attempts = existing_case.capture_attempts or 0
    case.order_id = existing_case.order_id or case.order_id
    case.approval_url = existing_case.approval_url or case.approval_url
    case.capture_id = existing_case.capture_id or case.capture_id
    case.payer_id = existing_case.payer_id or case.payer_id
    case.observed_payer_country = existing_case.observed_payer_country or case.observed_payer_country
    case.quote = existing_case.quote or case.quote
    case.paypal_evidence = existing_case.paypal_evidence or case.paypal_evidence
    case.reconciliation = existing_case.reconciliation or case.reconciliation
    case.paypal_error = existing_case.paypal_error or case.paypal_error
    case.paypal_issue = existing_case.paypal_issue or case.paypal_issue
    case.paypal_operation = existing_case.paypal_operation or case.paypal_operation
    case.paypal_debug_id = existing_case.paypal_debug_id or case.paypal_debug_id
    case.pilot_metadata = existing_case.pilot_metadata or case.pilot_metadata
    case.expected_payer_region = existing_case.expected_payer_region or case.expected_payer_region
    case.expected_surcharge_components = (
        existing_case.expected_surcharge_components or case.expected_surcharge_components
    )
    case.expected_surcharge_amount = existing_case.expected_surcharge_amount or case.expected_surcharge_amount
    return case


def _execute_plan(
    run_id: str,
    plan: list[Case],
    config: RunConfig,
    merchant_accounts: dict[str, Account],
    buyer_accounts: dict[str, Account],
    adapter: QuoteAdapter,
    oauth_cache: OAuthCache,
    *,
    resume: bool = False,
    extra_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the prepared plan, save results, build reports, and return the summary."""
    save_json(
        Path("artifacts/paypal-sandbox") / run_id / "run-config.json",
        sanitize_dict(config.model_dump()),
    )

    save_configuration_summary(
        run_id,
        {
            "csv_path": redact_path(config.accounts_csv),
            "merchant_count": len(merchant_accounts),
            "buyer_count": len(buyer_accounts),
            "profile": config.profile,
            "run_id": run_id,
        },
    )

    results: dict[str, Any] = {"run_id": run_id, "cases": []}
    existing_by_id: dict[str, dict[str, Any]] = {}

    if resume:
        existing_results = load_results(run_id)
        existing_by_id = {c["case_id"]: c for c in existing_results.get("cases", [])}

    mismatch_break = False
    for case in plan:
        if config.case_id and case.case_id != config.case_id:
            continue

        existing = existing_by_id.get(case.case_id)
        if existing and not config.retry_failed:
            case = _merge_existing_case(case, existing)

        result = _run_case(
            case=case,
            config=config,
            merchant_accounts=merchant_accounts,
            buyer_accounts=buyer_accounts,
            adapter=adapter,
            oauth_cache=oauth_cache,
        )

        existing_by_id[case.case_id] = result
        results["cases"] = list(existing_by_id.values()) if resume else results["cases"] + [result]
        save_results(run_id, results)

        rec = result.get("reconciliation", {}) or {}
        if (
            rec.get("status")
            in {
                "fee_mismatch",
                "net_amount_mismatch",
                "currency_mismatch",
                "buyer_country_mismatch",
            }
            and not config.continue_after_mismatch
        ):
            click.echo(f"Stopping after first mismatch: {case.case_id} ({rec['status']})")
            mismatch_break = True
            break

    summary = build_summary(run_id)
    if extra_summary:
        summary.update(extra_summary)
    save_summary(run_id, summary)
    save_summary_markdown(run_id, summary, config.accounts_csv)
    save_junit(run_id, summary)
    output = {k: v for k, v in summary.items() if k != "cases"}
    output["stopped_after_first_mismatch"] = mismatch_break
    return output


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

    # 1. Quote (re-use if already computed during planning or a previous run).
    if not case.quote:
        quote_result = _build_quote(case, merchant, buyer, adapter)
        if quote_result:
            return quote_result

    if config.dry_run:
        case.status = CaseStatus.PREDICTION_READY
        return _case_dict(case)

    # 2/3. Order creation and buyer approval share one callback server so the
    # redirect URL embedded in the created order matches the listener.
    callback: CallbackServer | None = None
    if not case.order_id or case.status in {CaseStatus.PLANNED, CaseStatus.PREDICTION_READY, CaseStatus.ORDER_CREATED}:
        callback = CallbackServer(expected_token="")
        callback.start()
    try:
        if not case.order_id:
            create_result = _create_order(case, merchant, oauth_cache, callback, payload_variant=config.payload_variant)
            if create_result:
                return create_result

        if case.status in {CaseStatus.PLANNED, CaseStatus.PREDICTION_READY, CaseStatus.ORDER_CREATED}:
            approve_result = _approve_order(case, buyer, config, callback)
            if approve_result:
                return approve_result
    finally:
        if callback:
            callback.stop()

    # 4. Capture and evidence extraction.
    if case.status == CaseStatus.BUYER_APPROVED:
        capture_result = _capture(case, merchant, oauth_cache)
        if capture_result:
            return capture_result

    # 5. Reconciliation.
    if case.status == CaseStatus.CAPTURED and not case.reconciliation:
        reconcile_result = _reconcile_case(case, merchant, buyer)
        if reconcile_result:
            return reconcile_result

    if case.status == CaseStatus.RECONCILED:
        return _case_dict(case)

    case.status = CaseStatus.FAILED
    return _case_dict(case, error=f"Unhandled case status: {case.status}")


def _build_quote(
    case: Case,
    merchant: Account,
    buyer: Account,
    adapter: QuoteAdapter,
) -> dict[str, Any] | None:
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
        request = quote.get("_request", {})
        transaction = request.get("transaction", {})
        case.expected_payer_region = transaction.get("payer_region")
        components = quote.get("components", [])
        surcharge_components = [c for c in components if c.get("type") == "surcharge"]
        case.expected_surcharge_components = len(surcharge_components)
        if surcharge_components:
            case.expected_surcharge_amount = surcharge_components[0].get("amount")
    except QuoteResolutionError as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=str(exc), reconciliation_status=exc.status)
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Library quote failed: {exc}")
    return None


def _create_order(
    case: Case,
    merchant: Account,
    oauth_cache: OAuthCache,
    callback: CallbackServer | None,
    payload_variant: str = "application_context",
) -> dict[str, Any] | None:
    if not case.request_id_create:
        case.request_id_create = generate_request_id(case.run_id, case.case_id, "create", case.create_attempts)

    try:
        assert merchant.client_id and merchant.secret
        token = fetch_token(oauth_cache, merchant.client_id, merchant.secret, merchant.country_code)
    except OAuthError as exc:
        case.status = CaseStatus.FAILED
        case.paypal_error = {"oauth_status": exc.status.value}
        if exc.status in {OAuthProbeStatus.INVALID_CLIENT, OAuthProbeStatus.AUTHENTICATION_FAILED}:
            case.reconciliation = {
                "status": ReconciliationStatus.AUTHENTICATION_FAILED.value,
                "delta_minor_units": None,
                "root_cause": f"OAuth failed: {exc.status.value}",
            }
        else:
            case.reconciliation = {
                "status": ReconciliationStatus.PAYPAL_API_FAILURE.value,
                "delta_minor_units": None,
                "root_cause": f"OAuth failed: {exc.status.value}",
            }
        return _case_dict(case, error=f"OAuth failed: {exc}")
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"OAuth failed: {exc}")

    client = PayPalClient(token=token)
    invoice_id = f"{case.run_id}-{case.case_id}"
    if callback is None:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error="No callback server available for order creation")

    try:
        payload = build_order_payload(
            amount=case.amount,
            currency=case.currency,
            return_url=callback.return_url,
            cancel_url=callback.cancel_url,
            reference_id=case.case_id,
            invoice_id=invoice_id,
            custom_id=case.case_id,
            brand_name="PayPal Sandbox Validation",
            form=payload_variant,
        )
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Failed to build order payload: {exc}")

    try:
        order = client.create_order(payload, request_id=case.request_id_create)
        case.order_id = order.get("id")
        case.status = CaseStatus.ORDER_CREATED
        case.create_attempts += 1
        save_sanitized_order(case.run_id, case.case_id, order)

        if not case.order_id:
            return _case_dict(case, error="Order response missing id")

        callback.update_expected_token(case.order_id)
        case.approval_url = extract_approval_url(order)
    except PayPalAPIError as exc:
        _record_paypal_error(case, exc)
        return _case_dict(case)
    return None


def _approve_order(
    case: Case,
    buyer: Account,
    config: RunConfig,
    callback: CallbackServer | None,
) -> dict[str, Any] | None:
    if not case.approval_url or not case.order_id:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error="Cannot approve order: missing approval URL or order id")

    screenshot_dir = Path("artifacts/paypal-sandbox") / case.run_id / "screenshots"
    approval_result = approve_order(
        buyer=buyer,
        approval_url=case.approval_url,
        amount=case.amount,
        currency=case.currency,
        order_token=case.order_id,
        headless=not (config.headful or config.headed),
        slow_mo=config.slow_mo,
        screenshot_dir=screenshot_dir,
        case_id=case.case_id,
        callback_server=callback,
    )
    if approval_result["status"] != "approved":
        case.status = CaseStatus.FAILED
        case.paypal_issue = approval_result.get("issue")
        case.paypal_operation = approval_result.get("operation")
        if approval_result.get("evidence"):
            case.pilot_metadata = {**case.pilot_metadata, "checkout_evidence": approval_result["evidence"]}
            case.paypal_error = {
                "message": approval_result.get("error"),
                "checkout_evidence": approval_result["evidence"],
            }
        else:
            case.paypal_error = {"message": approval_result.get("error")}
        return _case_dict(
            case,
            error=approval_result.get("error"),
            reconciliation_status=approval_result["status"],
        )
    case.status = CaseStatus.BUYER_APPROVED
    return None


def _capture(
    case: Case,
    merchant: Account,
    oauth_cache: OAuthCache,
) -> dict[str, Any] | None:
    from paypal_sandbox_validation import capture as capture_mod

    if not case.request_id_capture:
        case.request_id_capture = generate_request_id(case.run_id, case.case_id, "capture", case.capture_attempts)

    try:
        assert merchant.client_id and merchant.secret
        token = fetch_token(oauth_cache, merchant.client_id, merchant.secret, merchant.country_code)
    except OAuthError as exc:
        case.status = CaseStatus.FAILED
        case.paypal_error = {"oauth_status": exc.status.value}
        if exc.status in {OAuthProbeStatus.INVALID_CLIENT, OAuthProbeStatus.AUTHENTICATION_FAILED}:
            case.reconciliation = {
                "status": ReconciliationStatus.AUTHENTICATION_FAILED.value,
                "delta_minor_units": None,
                "root_cause": f"OAuth failed: {exc.status.value}",
            }
        else:
            case.reconciliation = {
                "status": ReconciliationStatus.PAYPAL_API_FAILURE.value,
                "delta_minor_units": None,
                "root_cause": f"OAuth failed: {exc.status.value}",
            }
        return _case_dict(case, error=f"OAuth failed: {exc}")
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"OAuth failed: {exc}")

    client = PayPalClient(token=token)
    if not case.order_id:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error="Cannot capture: missing order id")
    try:
        evidence = capture_mod.capture_order(client, case.order_id, case.request_id_capture)
    except PayPalAPIError as exc:
        _record_paypal_error(case, exc)
        return _case_dict(case)
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Capture failed: {exc}")

    case.capture_id = mask_value("capture_id", evidence.get("capture_id"))
    case.paypal_evidence = sanitize_dict(evidence)
    case.payer_id = evidence.get("payer_id")
    case.observed_payer_country = evidence.get("payer_country")
    case.capture_attempts += 1
    case.status = CaseStatus.CAPTURED
    save_case(case.run_id, case)
    return None


def _reconcile_case(
    case: Case,
    merchant: Account,
    buyer: Account,
) -> dict[str, Any] | None:
    try:
        rec_result = reconcile(
            case.paypal_evidence or {},
            case.quote or {},
            merchant.country_code,
            buyer.country_code,
            case.observed_payer_country,
        )
    except Exception as exc:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error=f"Reconciliation failed: {exc}")

    case.reconciliation = rec_result.model_dump()
    case.status = CaseStatus.RECONCILED
    save_case(case.run_id, case)
    return _case_dict(case)


def _record_paypal_error(case: Case, exc: PayPalAPIError) -> None:
    from paypal_sandbox_validation.error_classification import classify_paypal_api_error

    status, safe, detail = classify_paypal_api_error(exc)
    case.paypal_error = safe
    case.paypal_issue = safe.get("issue") or safe.get("error") or safe.get("name")
    case.paypal_operation = safe.get("operation")
    case.paypal_debug_id = safe.get("debug_id")
    case.status = CaseStatus.FAILED
    case.reconciliation = {
        "status": status.value,
        "delta_minor_units": None,
        "root_cause": detail,
    }


def _case_dict(
    case: Case,
    error: str | None = None,
    reconciliation_status: str | None = None,
) -> dict[str, Any]:
    if error and not case.paypal_error:
        case.paypal_error = {"message": error}
    if reconciliation_status:
        rec = case.reconciliation or {}
        rec["status"] = reconciliation_status
        case.reconciliation = rec
    if (
        error
        and case.status == CaseStatus.FAILED
        and (not case.reconciliation or not case.reconciliation.get("status"))
    ):
        rec = case.reconciliation or {}
        rec["status"] = ReconciliationStatus.PAYPAL_API_FAILURE.value
        case.reconciliation = rec
    return case.model_dump()


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


@cli.command("diagnose")
@click.option("--resume", type=str, required=True, help="Original run containing the mismatch case.")
@click.option("--case-id", type=str, required=True, help="Case to diagnose.")
@click.option(
    "--observations-run-id",
    type=str,
    default=None,
    help="Optional run with GBP 1/10/100 and control captures. If omitted, a bounded diagnostic plan is executed.",
)
@click.option("--accounts-csv", type=click.Path(exists=True, dir_okay=False), default=_env_csv_default)
@click.option("--headful", is_flag=True)
@click.option("--continue-after-mismatch", is_flag=True)
@click.option("--max-new-captures", type=int, default=5)
def diagnose_cmd(
    resume: str,
    case_id: str,
    observations_run_id: str | None,
    accounts_csv: str,
    headful: bool,
    continue_after_mismatch: bool,
    max_new_captures: int,
) -> None:
    """Diagnose a real PayPal Sandbox fee mismatch."""
    from . import diagnostics
    from .planner import build_diagnostic_plan

    original = diagnostics.load_original_case(resume, case_id)
    validation = diagnostics.validate_case_constraints(original)

    if not validation["valid"]:
        decomposition = diagnostics.decompose_case(original)
        root_cause = {
            "category": validation["classification"],
            "confidence": "confirmed",
            "explanation": "Pre-flight validation failed.",
        }
        observations: list[dict[str, Any]] = []
        reports = diagnostics.generate_diagnostic_reports(
            resume,
            case_id,
            original,
            decomposition,
            {"candidates": [], "best": None, "stable_linear_formula_found": False},
            root_cause,
            observations,
            None,
        )
        click.echo(
            json.dumps(
                {"status": validation["classification"], "reports": {k: str(v) for k, v in reports.items()}}, indent=2
            )
        )
        return

    decomposition = diagnostics.decompose_case(original)

    accounts = parse_accounts_csv(accounts_csv)
    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}

    if observations_run_id:
        obs_run = observations_run_id
        observations = diagnostics.build_observations_from_run(
            observations_run_id, original.merchant_country, currency=original.currency
        )
    else:
        adapter = QuoteAdapter()
        obs_run = generate_run_id()
        diagnostic_amounts = ["1.00", "100.00"]
        # GBP 10.00 original case can be reused; controls use 10.00
        control_buyers = ["AU", "GB", "DE", "US"]
        plan = build_diagnostic_plan(
            run_id=obs_run,
            merchant_country=original.merchant_country,
            diagnostic_amounts=diagnostic_amounts,
            control_buyers=control_buyers,
            currency=original.currency,
            adapter=adapter,
            max_new_captures=max_new_captures,
        )
        save_plan(obs_run, plan)
        config = RunConfig(
            accounts_csv=accounts_csv,
            profile="diagnostic",
            headful=headful,
            continue_after_mismatch=continue_after_mismatch,
        )
        _execute_plan(
            run_id=obs_run,
            plan=plan,
            config=config,
            merchant_accounts=merchant_accounts,
            buyer_accounts=buyer_accounts,
            adapter=adapter,
            oauth_cache=OAuthCache(),
        )
        observations = diagnostics.build_observations_from_run(
            obs_run, original.merchant_country, currency=original.currency
        )

    # Augment observations with the original case if not already present.
    orig_ev = original.paypal_evidence or {}
    orig_gross = orig_ev.get("gross_amount", {})
    orig_fee = orig_ev.get("paypal_fee", {})
    if orig_gross.get("value"):
        original_obs = {
            "amount": orig_gross.get("value"),
            "currency": orig_gross.get("currency_code"),
            "paypal_fee": orig_fee.get("value"),
            "buyer_country": original.buyer_country,
            "observed_payer_country": orig_ev.get("payer_country"),
        }
        if not any(
            o["amount"] == original_obs["amount"] and o.get("buyer_country") == original_obs["buyer_country"]
            for o in observations
        ):
            observations.append(original_obs)

    base_pct = diagnostics._decimal(decomposition["library_base"]["percentage"])
    surcharge_pct = diagnostics._decimal(decomposition["library_surcharge"]["percentage"])
    fixed = diagnostics._decimal(decomposition["library_base"]["direct_fixed_amount"])
    formula = diagnostics.infer_formula(observations, base_pct=base_pct, surcharge_pct=surcharge_pct, fixed=fixed)

    account_config = _merchant_account_config(accounts_csv, original.merchant_country)
    root_cause = diagnostics.classify_root_cause(original, decomposition, formula, account_config)

    reports = diagnostics.generate_diagnostic_reports(
        resume, case_id, original, decomposition, formula, root_cause, observations, account_config
    )

    _write_observation_fixture(original, observations, formula, root_cause, reports["diagnostic_json"].parent)

    click.echo("Diagnostic reports generated.")
    click.echo(f"Artifact path: {reports['diagnostic_json'].parent}")
    click.echo(
        json.dumps(
            {
                "observations_run_id": obs_run,
                "root_cause": root_cause,
                "reports": {k: str(v) for k, v in reports.items()},
            },
            indent=2,
        )
    )


def _default_currency(merchant_country: str) -> str:
    from paypal_sandbox_validation.configuration import currency_for_country

    try:
        return currency_for_country(merchant_country)
    except Exception:
        return "USD"


def _create_and_associate_order(
    merchant: Account,
    amount: str,
    currency: str,
    oauth_cache: OAuthCache,
    callback: CallbackServer,
    payload_variant: str = "application_context",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Create an order, retrieve it, and return order, payload, and payee info.

    Returns (order, payload, error). On success error is None.
    """
    if not merchant.client_id or not merchant.secret:
        return None, None, {"error": "missing client_id or secret", "association_status": "missing_credentials"}

    try:
        token = fetch_token(oauth_cache, merchant.client_id, merchant.secret, merchant.country_code)
    except OAuthError as exc:
        return None, None, {"error": f"OAuth failed: {exc.status.value}", "association_status": "oauth_failed"}

    client = PayPalClient(token=token)
    run_id = generate_run_id()
    reference_id = f"verify-{merchant.country_code}-{run_id}"

    try:
        payload = build_order_payload(
            amount=amount,
            currency=currency,
            return_url=callback.return_url,
            cancel_url=callback.cancel_url,
            reference_id=reference_id,
            invoice_id=reference_id,
            custom_id=reference_id,
            brand_name="PayPal Sandbox Validation",
            form=payload_variant,
        )
    except Exception as exc:
        return None, None, {"error": f"Failed to build order payload: {exc}", "association_status": "payload_error"}

    request_id = generate_request_id(run_id, reference_id, "create", 0)
    try:
        order = client.create_order(payload, request_id=request_id)
    except PayPalAPIError as exc:
        return (
            None,
            payload,
            {
                "error": f"PayPal API create order failed: {exc}",
                "paypal_error": extract_paypal_error_fields(exc),
                "association_status": "paypal_api_error",
            },
        )

    order_id = order.get("id")
    if not order_id:
        return None, payload, {"error": "Order response missing id", "association_status": "malformed_response"}

    try:
        order = client.get_order(order_id)
    except PayPalAPIError as exc:
        return (
            order,
            payload,
            {
                "error": f"PayPal API get order failed: {exc}",
                "paypal_error": extract_paypal_error_fields(exc),
                "association_status": "paypal_api_error",
            },
        )

    return order, payload, None


def _order_payee_association(order: dict[str, Any], merchant: Account) -> tuple[bool, dict[str, Any]]:
    """Compare order payee with configured merchant email.

    Returns (verified, sanitized_evidence). Does not log emails.
    """
    payee = extract_payee_info(order)
    payee_email = payee.get("email_address")
    payee_merchant_id = payee.get("merchant_id")
    configured_email = merchant.primary_email_alias
    payee_present = bool(payee_email or payee_merchant_id)
    payee_email_matches = bool(payee_email and configured_email and payee_email.lower() == configured_email.lower())

    # Sanitize the order for any artifact: keep only presence booleans, no addresses or IDs.
    sanitized_order = sanitize_paypal_order(order)
    sanitized_order["payee_present"] = payee_present
    sanitized_order["payee_email_matches"] = payee_email_matches
    return payee_email_matches, sanitized_order


def _merchant_account_config(accounts_csv: str, merchant_country: str) -> dict[str, Any] | None:
    """Return non-secret configuration hints from the account CSV."""
    accounts = parse_accounts_csv(accounts_csv)
    for a in accounts:
        if a.country_code == merchant_country and a.is_business():
            return {
                "sandbox_country": a.country_code,
                "account_type": a.account_type,
                "pp_balance": a.pp_balance,
                "add_bank": a.add_bank,
                "cc_type": a.cc_type,
                "payment_card": a.payment_card,
            }
    return None


def _write_observation_fixture(
    original: Case,
    observations: list[dict[str, Any]],
    formula: dict[str, Any],
    root_cause: dict[str, Any],
    output_dir: Path,
) -> None:
    """Write secret-free observation fixture for later review."""
    fixture_dir = Path("artifacts/paypal-sandbox-observations")
    fixture_dir.mkdir(parents=True, exist_ok=True)
    quote = original.quote or {}
    meta = quote.get("_schedule_metadata") or {}
    fixture = {
        "provider": "paypal",
        "environment": "sandbox",
        "merchant_country": original.merchant_country,
        "buyer_country": original.buyer_country,
        "observed_payer_country": (original.paypal_evidence or {}).get("payer_country"),
        "amount": {"value": original.amount, "currency": original.currency},
        "paypal_fee": {
            "value": (original.paypal_evidence or {}).get("paypal_fee", {}).get("value"),
            "currency": original.currency,
        },
        "expected_library_fee": {"value": quote.get("processing_fee", {}).get("value"), "currency": original.currency},
        "product_id": original.product_id,
        "variant_id": original.variant_id,
        "payer_region": meta.get("payer_region"),
        "rule_ids": [r.get("rule_id") for r in quote.get("matched_rules", [])],
        "schedule_ids": [
            meta.get("fixed_fee_schedule_id"),
            meta.get("international_surcharge_schedule_id"),
        ],
        "data_revision": quote.get("data", {}).get("content_sha256"),
        "crawler_revision": quote.get("data", {}).get("data_ref"),
        "result": "match"
        if root_cause.get("category") != "sandbox_account_configuration"
        else "account_configuration_difference",
        "diagnostic_run_id": output_dir.parent.name,
    }
    fixture_path = fixture_dir / f"{original.run_id}-{original.case_id}.json"
    fixture_path.write_text(json.dumps(fixture, indent=2, sort_keys=True))


@cli.command("verify-merchant-association")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--merchant", type=str, default="DE")
@click.option("--amount", type=str, default="1.00")
@click.option("--currency", type=str, default=None)
@click.option(
    "--payload-variant",
    type=click.Choice(["application_context", "payment_source"], case_sensitive=False),
    default="application_context",
)
def verify_merchant_association_cmd(
    accounts_csv: str,
    merchant: str,
    amount: str,
    currency: str | None,
    payload_variant: str,
) -> None:
    """Create a DE order, retrieve it, and verify the payee belongs to the configured merchant."""
    accounts = parse_accounts_csv(accounts_csv)
    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    merchant_account = merchant_accounts.get(merchant.upper())
    if not merchant_account:
        click.echo(json.dumps({"merchant_country": merchant, "association_status": "merchant_not_found"}, indent=2))
        sys.exit(1)

    currency = currency or _default_currency(merchant_account.country_code)
    oauth_cache = OAuthCache()
    with CallbackServer(expected_token="") as callback:
        order, payload, error = _create_and_associate_order(
            merchant_account,
            amount=amount,
            currency=currency,
            oauth_cache=oauth_cache,
            callback=callback,
            payload_variant=payload_variant,
        )

    if error:
        result = {
            "merchant_country": merchant_account.country_code,
            "oauth_valid": error["association_status"] != "oauth_failed",
            "payee_present": False,
            "payee_email_matches": False,
            "association_status": error["association_status"],
            "payload_variant": payload_variant,
            "payload_signature": order_payload_signature(payload) if payload else None,
        }
        click.echo(json.dumps(result, indent=2))
        sys.exit(1)

    assert order is not None
    payee_email_matches, sanitized_order = _order_payee_association(order, merchant_account)
    run_id = generate_run_id()
    artifact_dir = artifact_root() / run_id / "merchant-association"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    save_json(
        artifact_dir / f"{merchant_account.country_code}-{payload_variant}.json",
        {
            "merchant_country": merchant_account.country_code,
            "payee_present": sanitized_order.get("payee_present"),
            "payee_email_matches": payee_email_matches,
            "association_status": "associated" if payee_email_matches else "mismatch",
            "payload_signature": order_payload_signature(payload if payload is not None else {}),
            "order": sanitized_order,
        },
    )

    result = {
        "merchant_country": merchant_account.country_code,
        "oauth_valid": True,
        "payee_present": sanitized_order.get("payee_present"),
        "payee_email_matches": payee_email_matches,
        "association_status": "associated" if payee_email_matches else "mismatch",
        "payload_variant": payload_variant,
    }
    click.echo(json.dumps(result, indent=2))


@cli.command("create-manual-approval-case")
@click.option(
    "--accounts-csv",
    type=click.Path(exists=True, dir_okay=False),
    default=_env_csv_default,
)
@click.option("--merchant", type=str, default="DE")
@click.option("--buyer", type=str, default="DE")
@click.option("--amount", type=str, default="1.00")
@click.option("--currency", type=str, default=None)
@click.option(
    "--payload-variant",
    type=click.Choice(["application_context", "payment_source"], case_sensitive=False),
    default="application_context",
)
@click.option("--wait-seconds", type=int, default=300)
@click.option("--poll-interval", type=int, default=5)
@click.option("--show-approval-url", is_flag=True, default=True)
def create_manual_approval_case_cmd(
    accounts_csv: str,
    merchant: str,
    buyer: str,
    amount: str,
    currency: str | None,
    payload_variant: str,
    wait_seconds: int,
    poll_interval: int,
    show_approval_url: bool,
) -> None:
    """Create an order and wait for manual browser approval before capturing.

    The approval URL is printed for the user; it is not persisted.
    """
    import time

    accounts = parse_accounts_csv(accounts_csv)
    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    merchant_account = merchant_accounts.get(merchant.upper())
    buyer_account = buyer_accounts.get(buyer.upper())
    if not merchant_account:
        click.echo(json.dumps({"error": "merchant not found"}, indent=2), err=True)
        sys.exit(1)
    if not buyer_account:
        click.echo(json.dumps({"error": "buyer not found"}, indent=2), err=True)
        sys.exit(1)

    currency = currency or _default_currency(merchant_account.country_code)
    run_id = generate_run_id()
    case_id = f"manual-{merchant_account.country_code}-{buyer_account.country_code}-{run_id}"
    artifact_dir = artifact_root() / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    oauth_cache = OAuthCache()
    callback = CallbackServer(expected_token="")
    callback.start()
    try:
        order, payload, error = _create_and_associate_order(
            merchant_account,
            amount=amount,
            currency=currency,
            oauth_cache=oauth_cache,
            callback=callback,
            payload_variant=payload_variant,
        )
    finally:
        pass  # keep callback alive during polling; stopped below
    if error:
        click.echo(json.dumps({"error": error, "payload_variant": payload_variant}, indent=2))
        callback.stop()
        sys.exit(1)

    assert order is not None
    order_id = order.get("id")
    if not order_id:
        click.echo(json.dumps({"error": "Order response missing id", "payload_variant": payload_variant}, indent=2))
        callback.stop()
        sys.exit(1)
    if not merchant_account.client_id or not merchant_account.secret:
        click.echo(json.dumps({"error": "missing client_id or secret"}, indent=2), err=True)
        callback.stop()
        sys.exit(1)

    callback.update_expected_token(order_id)
    approval_url = extract_approval_url(order)
    payload_signature = order_payload_signature(payload if payload is not None else {})

    sanitized_order = sanitize_paypal_order(order)
    save_json(artifact_dir / "order-created.json", sanitized_order)
    save_json(artifact_dir / "payload-signature.json", payload_signature)

    if show_approval_url:
        click.echo("Approve this order in a normal browser:")
        click.echo(approval_url)
        click.echo(f"Waiting up to {wait_seconds}s for the order to be manually approved...")

    token = fetch_token(oauth_cache, merchant_account.client_id, merchant_account.secret, merchant_account.country_code)
    client = PayPalClient(token=token)

    deadline = time.time() + wait_seconds
    status = "CREATED"
    final_order: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            final_order = client.get_order(order_id)
        except PayPalAPIError as exc:
            click.echo(f"Warning: get_order failed: {exc}", err=True)
            time.sleep(poll_interval)
            continue
        status = final_order.get("status", status)
        if status == "APPROVED":
            break
        time.sleep(poll_interval)

    if status != "APPROVED":
        result = {
            "merchant_country": merchant_account.country_code,
            "buyer_country": buyer_account.country_code,
            "amount": amount,
            "currency": currency,
            "payload_variant": payload_variant,
            "order_status": status,
            "manual_approval": "timeout",
            "payload_signature": payload_signature,
        }
        save_json(artifact_dir / "manual-approval-timeout.json", result)
        click.echo(json.dumps(result, indent=2))
        callback.stop()
        return

    # Capture the approved order.
    request_id = generate_request_id(run_id, case_id, "capture", 0)
    try:
        capture = client.capture_order(order_id, request_id=request_id)
    except PayPalAPIError as exc:
        result = {
            "merchant_country": merchant_account.country_code,
            "buyer_country": buyer_account.country_code,
            "amount": amount,
            "currency": currency,
            "payload_variant": payload_variant,
            "order_status": status,
            "manual_approval": "approved",
            "capture_status": "failed",
            "paypal_error": extract_paypal_error_fields(exc),
            "payload_signature": payload_signature,
        }
        save_json(artifact_dir / "capture-failed.json", result)
        click.echo(json.dumps(result, indent=2))
        callback.stop()
        return

    # Build evidence and reconcile.
    from paypal_sandbox_validation.diagnostics import validate_case_constraints

    capture_details = (capture.get("purchase_units") or [{}])[0].get("payments", {}).get("captures") or [{}]
    capture_detail = capture_details[0] if capture_details else {}
    capture_id = capture_detail.get("id")
    breakdown = capture_detail.get("seller_receivable_breakdown", {}) or {}
    payer = capture.get("payer", {}) or {}
    payer_country = (payer.get("address") or {}).get("country_code")
    if not payer_country:
        payer_country = (payer.get("payer_info") or {}).get("country_code")

    paypal_evidence = {
        "status": "COMPLETED",
        "gross_amount": breakdown.get("gross_amount"),
        "paypal_fee": breakdown.get("paypal_fee"),
        "net_amount": breakdown.get("net_amount"),
        "payer_country": payer_country,
    }

    adapter = QuoteAdapter()
    try:
        quote = adapter.build_quote(
            merchant_account.country_code,
            buyer_account.country_code,
            amount,
            currency,
        )
    except Exception as exc:
        quote = None
        click.echo(f"Warning: library quote failed: {exc}", err=True)

    case = Case(
        case_id=case_id,
        run_id=run_id,
        merchant_country=merchant_account.country_code,
        buyer_country=buyer_account.country_code,
        amount=amount,
        currency=currency,
        product_id=quote.get("_scenario", {}).get("product_id") if quote else "",
        variant_id=quote.get("_scenario", {}).get("variant_id") if quote else "",
        status=CaseStatus.CAPTURED,
        order_id=order_id,
        capture_id=capture_id,
        paypal_evidence=paypal_evidence,
        quote=quote,
    )

    if quote:
        result = reconcile(
            paypal_evidence=paypal_evidence,
            quote=quote,
            merchant_country=merchant_account.country_code,
            buyer_country=buyer_account.country_code,
            observed_payer_country=payer_country,
        )
        case.reconciliation = result.model_dump(exclude_none=True)
        case.status = CaseStatus.RECONCILED

    validation = (
        validate_case_constraints(case) if quote else {"valid": False, "classification": "library_not_calculable"}
    )
    if not validation["valid"]:
        case.status = CaseStatus.FAILED
        case.paypal_issue = validation["classification"]

    # Persist a secret-free report.
    report = {
        "run_id": run_id,
        "case_id": case_id,
        "merchant_country": merchant_account.country_code,
        "buyer_country": buyer_account.country_code,
        "amount": amount,
        "currency": currency,
        "payload_variant": payload_variant,
        "manual_approval": "approved",
        "capture_status": "completed",
        "paypal_fee": paypal_evidence.get("paypal_fee"),
        "library_fee": quote.get("processing_fee") if quote else None,
        "reconciliation": case.reconciliation,
        "validation": validation,
        "payload_signature": payload_signature,
    }
    save_json(artifact_dir / "manual-approval-capture.json", report)
    click.echo(json.dumps(report, indent=2))
    callback.stop()


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
    from pathlib import Path

    from paypal_sandbox_validation.qualification import (
        classify_manual_send_pricing,
        load_qualification_registry,
        save_qualification_registry,
        update_manual_send_qualification,
    )

    results = load_manual_results(run_id)
    cases = [Case.model_validate(c) for c in results.get("cases", [])]
    observation = classify_manual_send_pricing(cases)
    if not observation:
        click.echo("No classifiable manual-send observation found.", err=True)
        sys.exit(1)

    merchant_country = observation["merchant_country"]
    registry = load_qualification_registry(Path(qualification_registry))
    update_manual_send_qualification(registry, merchant_country, observation)
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


def _mark_diagnostic_sandbox_pricing(plan: list[Case], registry: dict[str, Any]) -> None:
    """Tag cases from sandbox-pricing merchants as diagnostic observations."""
    for case in plan:
        entry = registry.get(case.merchant_country, {})
        if entry.get("representative_for_public_rates") is False:
            case.pilot_metadata["diagnostic_sandbox_pricing"] = True
            observed = (entry.get("manual_send_observation") or {}).get("observed_account_formula", {})
            if observed.get("percentage"):
                case.pilot_metadata["account_specific_base_rate"] = (
                    f"{observed['percentage']}% + {observed['fixed']['currency']} {observed['fixed']['value']}"
                )


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
        expected = (Decimal(gross) * slope + intercept).quantize(Decimal("0.0001"))
        rounded = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        checks.append(
            {
                "case_id": case.case_id,
                "amount": str(gross),
                "observed_paypal_fee": str(observed),
                "expected_paypal_fee": str(rounded),
                "raw_expected": str(expected),
                "matches": rounded == Decimal(observed),
                "note": "historical supporting observation",
            }
        )
    return checks


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
