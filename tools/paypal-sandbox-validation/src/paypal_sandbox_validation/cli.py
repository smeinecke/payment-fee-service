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
from paypal_sandbox_validation.oauth import OAuthCache, OAuthError, OAuthProbeStatus, fetch_token, probe_credentials
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
from paypal_sandbox_validation.redaction import mask_value, redact_path, sanitize_dict
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
        "duplicate_accounts": len(validation["duplicate_accounts"]),
        "duplicate_client_ids": len(validation["duplicate_client_ids"]),
        "invalid_business_credentials": len(validation["invalid_business_credentials"]),
        "missing_business_credentials": len(validation["missing_business_credentials"]),
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

    def _allowed(m: str) -> bool:
        return (
            m in configured_merchants
            and m != "DE"
            and not is_merchant_excluded(m, registry, override=include_unsuitable)
        )

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
    if merchants:
        target_merchants = [m.strip().upper() for m in merchants.split(",") if m.strip()]
    else:

        def _allowed_qual(m: str) -> bool:
            return (
                m in merchant_accounts
                and m != "DE"
                and not is_merchant_excluded(m, registry, override=include_unsuitable)
            )

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
            create_result = _create_order(case, merchant, oauth_cache, callback)
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
