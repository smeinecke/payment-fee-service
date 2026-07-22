from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.configuration import (
    load_scenarios,
)
from paypal_sandbox_validation.models import (
    RunConfig,
)
from paypal_sandbox_validation.oauth import OAuthCache
from paypal_sandbox_validation.persistence import (
    load_plan,
    save_json,
    save_plan,
)
from paypal_sandbox_validation.planner import (
    build_plan,
    build_regional_pilot_plan,
    build_surcharge_pilot_plan,
    enrich_plan_with_products,
    ensure_surcharge_case,
    generate_run_id,
    plan_summary,
)
from paypal_sandbox_validation.qualification import (
    default_qualification_path,
    load_qualification_registry,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter

from . import _env_csv_default, cli
from .qualify import (
    _build_diagnostic_pilot_plan,
    _parse_requested_merchants,
    _select_target_merchants,
    _tag_diagnostic_sandbox_pricing,
    _tag_public_rate_validation,
)
from .runner import _execute_plan


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
@click.option(
    "--merchants",
    type=str,
    default=None,
    help="Comma-separated merchant countries (default: all eligible).",
)
@click.option(
    "--merchant",
    type=str,
    default=None,
    help="Single-merchant alias for --merchants.",
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
    help="Include only merchants with a sandbox-pricing classification as diagnostic cases.",
)
@click.option(
    "--diagnostic-amounts",
    type=str,
    default="1.00,10.00,100.00",
    help="Comma-separated diagnostic amounts (used with --diagnostic-sandbox-pricing).",
)
@click.option(
    "--diagnostic-control-buyers",
    type=str,
    default=",".join(["US", "GB", "DE", "JP", "BR", "HK", "IL", "ZA"]),
    help="Comma-separated control buyers for diagnostic matrix.",
)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def regional_pilot_cmd(
    accounts_csv: str,
    merchants: str | None,
    merchant: str | None,
    max_cases: int,
    headful: bool,
    headed: bool,
    slow_mo: int,
    continue_after_mismatch: bool,
    resume: str | None,
    include_unsuitable: bool,
    diagnostic_sandbox_pricing: bool,
    diagnostic_amounts: str,
    diagnostic_control_buyers: str,
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
    buyer_countries = {a.country_code for a in buyer_accounts.values()}
    registry = load_qualification_registry(Path(qualification_registry))

    requested = _parse_requested_merchants(merchants, merchant)
    regional_merchants = _select_target_merchants(
        requested=requested,
        configured_merchants=configured_merchants,
        registry=registry,
        include_unsuitable=include_unsuitable,
        diagnostic_sandbox_pricing=diagnostic_sandbox_pricing,
    )

    click.echo(f"Selected regional-pilot merchants: {regional_merchants}")

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
        if diagnostic_sandbox_pricing:
            amounts = [a.strip() for a in diagnostic_amounts.split(",") if a.strip()]
            controls = [b.strip().upper() for b in diagnostic_control_buyers.split(",") if b.strip()]
            plan = _build_diagnostic_pilot_plan(
                run_id=run_id,
                merchant_countries=regional_merchants,
                buyer_countries=buyer_countries,
                adapter=adapter,
                amounts=amounts,
                controls=controls,
                max_new_captures=max_cases,
            )
            _tag_diagnostic_sandbox_pricing(plan, registry)
            pilot_summary = {"diagnostic_merchants": regional_merchants, "amounts": amounts, "controls": controls}
        else:
            plan, pilot_summary = build_regional_pilot_plan(
                run_id=run_id,
                merchant_countries=regional_merchants,
                buyer_countries=buyer_countries,
                adapter=adapter,
                max_cases=max_cases,
            )
            _tag_public_rate_validation(plan, registry)
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
