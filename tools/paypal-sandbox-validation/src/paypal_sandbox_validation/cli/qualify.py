from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.configuration import (
    currency_for_country,
)
from paypal_sandbox_validation.models import (
    Account,
    Case,
    CaseStatus,
    QualificationStatus,
    ReconciliationStatus,
    RunConfig,
)
from paypal_sandbox_validation.oauth import OAuthCache
from paypal_sandbox_validation.persistence import (
    load_plan,
    load_results,
    save_plan,
)
from paypal_sandbox_validation.planner import (
    build_diagnostic_plan,
    generate_run_id,
)
from paypal_sandbox_validation.qualification import (
    build_qualification_plan,
    build_validation_plan,
    classify_qualification,
    default_qualification_path,
    is_diagnostic_sandbox_pricing_merchant,
    is_merchant_excluded,
    load_qualification_registry,
    promote_observation_fixtures,
    qualification_summary,
    save_qualification_registry,
    save_qualification_report,
    save_validation_report,
    validation_summary,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.reconciliation import reconcile

from . import _env_csv_default, cli
from .diagnose import _merchant_account_config
from .runner import _execute_plan

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
@click.option(
    "--merchant",
    type=str,
    default=None,
    help="Single-merchant alias for --merchants.",
)
@click.option("--max-merchants", type=int, default=0)
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
    merchant: str | None,
    max_merchants: int,
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
    configured_merchants = set(merchant_accounts.keys())
    buyer_accounts = {a.country_code: a for a in accounts if a.is_personal()}
    buyer_countries = {a.country_code for a in buyer_accounts.values()}

    registry = load_qualification_registry(Path(qualification_registry))

    requested = _parse_requested_merchants(merchants, merchant)
    target_merchants = _select_target_merchants(
        requested=requested,
        configured_merchants=configured_merchants,
        registry=registry,
        include_unsuitable=include_unsuitable,
        diagnostic_sandbox_pricing=diagnostic_sandbox_pricing,
    )
    if max_merchants:
        target_merchants = target_merchants[:max_merchants]

    click.echo(f"Selected qualification merchants: {target_merchants}")

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
        if diagnostic_sandbox_pricing:
            _tag_diagnostic_sandbox_pricing(plan, registry)
        else:
            _tag_public_rate_validation(plan, registry)
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
@click.option(
    "--merchants",
    type=str,
    default=None,
    help="Comma-separated merchant countries to validate (default: all representative).",
)
@click.option(
    "--merchant",
    type=str,
    default=None,
    help="Single-merchant alias for --merchants.",
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
    merchants: str | None,
    merchant: str | None,
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

    requested = _parse_requested_merchants(merchants, merchant)
    representative = _filter_representative_merchants(
        requested=requested,
        merchant_accounts=merchant_accounts,
        registry=registry,
    )
    if max_merchants:
        representative = representative[:max_merchants]

    click.echo(f"Selected representative merchants for regional validation: {representative}")

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
        _tag_public_rate_validation(plan, registry)
        _attempt_public_rate_reuse(plan, registry, adapter)
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
    summary = validation_summary(cases, registry, fixture_paths=fixture_paths)
    click.echo("Regional validation complete.")
    click.echo(f"Run ID: {run_id}")
    click.echo(f"Report: {report_paths['md']}")
    click.echo(f"New positive fixtures: {summary.get('new_positive_fixtures_generated', 0)}")
    click.echo(json.dumps(summary, indent=2))


def _attempt_public_rate_reuse(
    plan: list[Case],
    registry: dict[str, Any],
    adapter: QuoteAdapter,
    fixtures_dir: Path | None = None,
) -> None:
    """Hydrate planned public-rate cases from existing positive observation fixtures.

    A fixture supplies historical PayPal evidence only. A new quote is built from
    the current dataset and the historical observation is reconciled against it.
    If the current prediction no longer matches, the case is recorded as a
    historical-observation current mismatch and is not promoted as a new fixture.
    """
    fixtures_dir = fixtures_dir or Path("artifacts/paypal-sandbox-observations")
    if not fixtures_dir.exists():
        return

    index: dict[tuple[str, str, str, str, str, str], list[tuple[Path, dict[str, Any]]]] = {}
    for fixture_path in fixtures_dir.glob("*.json"):
        try:
            fixture = json.loads(fixture_path.read_text())
        except Exception:
            continue
        if fixture.get("result") != ReconciliationStatus.MATCH:
            continue
        key = (
            fixture.get("merchant_country", ""),
            fixture.get("buyer_country", ""),
            str(fixture.get("amount", {}).get("value", "")),
            fixture.get("amount", {}).get("currency", ""),
            fixture.get("product_id", ""),
            fixture.get("variant_id", ""),
        )
        index.setdefault(key, []).append((fixture_path, fixture))

    for case in plan:
        if case.execution_classification != "public_rate_validation":
            continue
        entry = registry.get(case.merchant_country, {})
        if entry.get("status") != QualificationStatus.REPRESENTATIVE:
            continue
        key = (case.merchant_country, case.buyer_country, case.amount, case.currency, case.product_id, case.variant_id)
        candidates = index.get(key, [])
        if not candidates:
            continue
        # Prefer the most recently written fixture.
        candidates.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
        fixture_path, fixture = candidates[0]
        amount = str(fixture["amount"]["value"])
        currency = fixture["amount"]["currency"]
        fee = str(fixture["paypal_fee"]["value"])
        try:
            net = str(Decimal(amount) - Decimal(fee))
        except Exception:
            net = amount

        # Build a new quote from the current dataset revision and reconcile.
        try:
            new_quote = adapter.build_quote(case.merchant_country, case.buyer_country, amount, currency)
        except Exception:
            new_quote = {}
        case.quote = new_quote

        paypal_evidence = {
            "status": "COMPLETED",
            "gross_amount": {"value": amount, "currency_code": currency},
            "paypal_fee": {"value": fee, "currency_code": currency},
            "net_amount": {"value": net, "currency_code": currency},
            "payer_country": fixture.get("observed_payer_country"),
        }
        case.paypal_evidence = paypal_evidence
        case.observed_payer_country = fixture.get("observed_payer_country")

        rec = reconcile(
            paypal_evidence,
            new_quote,
            case.merchant_country,
            case.buyer_country,
            observed_payer_country=fixture.get("observed_payer_country"),
        )
        rec_data = rec.model_dump()
        if rec.status != ReconciliationStatus.MATCH:
            rec_data["status"] = ReconciliationStatus.HISTORICAL_OBSERVATION_CURRENT_MISMATCH.value
            rec_data["root_cause"] = "Historical observation does not match the current dataset prediction."
        case.reconciliation = rec_data
        case.status = CaseStatus.RECONCILED
        case.create_attempts = 0
        case.capture_attempts = 0
        case.paypal_operations_executed_in_current_run = 0
        case.observation_source = "historical_fixture"
        # order_id, capture_id and request IDs remain null for fixture-hydrated cases.

        source_meta = fixture.get("data_revision")
        current_meta = (new_quote.get("data") or {}).get("content_sha256")
        case.pilot_metadata["reused_observation"] = True
        case.pilot_metadata["reused_from_fixture"] = fixture_path.name
        case.pilot_metadata["historical_fixture_result"] = fixture.get("result")
        case.pilot_metadata["source_data_revision"] = source_meta
        case.pilot_metadata["current_data_revision"] = current_meta
        case.pilot_metadata["source_rule_ids"] = fixture.get("rule_ids")
        case.pilot_metadata["current_rule_ids"] = [r.get("rule_id") for r in new_quote.get("matched_rules", [])]
        case.pilot_metadata["source_schedule_ids"] = fixture.get("schedule_ids")
        case.pilot_metadata["current_schedule_ids"] = [
            (new_quote.get("_schedule_metadata") or {}).get("fixed_fee_schedule_id"),
            (new_quote.get("_schedule_metadata") or {}).get("international_surcharge_schedule_id"),
        ]
        delta_minor = rec_data.get("delta_minor_units")
        case.pilot_metadata["current_delta_minor_units"] = delta_minor
        case.pilot_metadata["current_absolute_delta_minor_units"] = (
            abs(delta_minor) if delta_minor is not None else None
        )


def _parse_requested_merchants(merchants: str | None, merchant: str | None) -> list[str] | None:
    """Return a list of requested merchant country codes, or None for default selection."""
    if merchants and merchant:
        raise click.UsageError("Use either --merchants or --merchant, not both.")
    if merchant:
        return [merchant.strip().upper()]
    if merchants:
        return [m.strip().upper() for m in merchants.split(",") if m.strip()]
    return None


def _filter_representative_merchants(
    requested: list[str] | None,
    merchant_accounts: dict[str, Account],
    registry: dict[str, Any],
) -> list[str]:
    """Resolve requested merchant codes to representative, configured merchants.

    Unknown or non-representative codes are rejected rather than silently skipped.
    """
    configured = set(merchant_accounts.keys())
    candidates = (
        requested
        if requested is not None
        else [m for m, q in sorted(registry.items()) if q.get("status") == QualificationStatus.REPRESENTATIVE]
    )

    selected: list[str] = []
    for m in candidates:
        if m not in configured:
            raise click.UsageError(f"Merchant {m} is not configured in the accounts CSV.")
        entry = registry.get(m, {})
        if entry.get("status") != QualificationStatus.REPRESENTATIVE:
            raise click.UsageError(f"Merchant {m} is not qualified as representative (status: {entry.get('status')}).")
        if m not in selected:
            selected.append(m)
    return selected


def _is_hard_excluded_for_diagnostic(entry: dict[str, Any]) -> bool:
    """Return True for statuses that are never eligible for a diagnostic run."""
    status = entry.get("status")
    return status in {
        QualificationStatus.ACCOUNT_CONFIGURATION_BLOCKED,
        QualificationStatus.SANDBOX_CHECKOUT_LIMITATION,
        QualificationStatus.DATASET_NOT_CALCULABLE,
        QualificationStatus.CAPABILITY_UNAVAILABLE,
        "compliance_violation",
    }


def _select_target_merchants(
    requested: list[str] | None,
    configured_merchants: set[str],
    registry: dict[str, Any],
    include_unsuitable: bool,
    diagnostic_sandbox_pricing: bool,
    default_candidates: list[str] | None = None,
) -> list[str]:
    """Resolve and validate the merchant list for regional-pilot and qualify runs."""
    if requested is not None:
        selected: list[str] = []
        for m in requested:
            if m not in configured_merchants:
                raise click.UsageError(f"Merchant {m} is not configured in the accounts CSV.")
            entry = registry.get(m, {})
            if include_unsuitable:
                selected.append(m)
            elif diagnostic_sandbox_pricing:
                if _is_hard_excluded_for_diagnostic(entry):
                    raise click.UsageError(f"Merchant {m} is not eligible for diagnostic sandbox-pricing mode.")
                selected.append(m)
            else:
                if entry.get("status") != QualificationStatus.REPRESENTATIVE:
                    raise click.UsageError(
                        f"Merchant {m} is not qualified as representative (status: {entry.get('status')})."
                    )
                selected.append(m)
        return selected

    # Default selection from the regional pilot list.
    candidates = default_candidates or REGIONAL_PILOT_MERCHANTS
    if include_unsuitable:
        return [m for m in candidates if m in configured_merchants]
    if diagnostic_sandbox_pricing:
        return [
            m
            for m in candidates
            if m in configured_merchants and is_diagnostic_sandbox_pricing_merchant(registry.get(m, {}))
        ]
    return [m for m in candidates if m in configured_merchants and not is_merchant_excluded(m, registry)]


def _build_diagnostic_pilot_plan(
    run_id: str,
    merchant_countries: list[str],
    buyer_countries: set[str],
    adapter: QuoteAdapter,
    amounts: list[str],
    controls: list[str],
    max_new_captures: int,
) -> list[Case]:
    """Build a diagnostic matrix for each selected merchant.

    The merchant's own country is used as the primary (domestic) buyer; the
    requested control buyers provide cross-border controls.
    """
    plan: list[Case] = []
    for merchant in merchant_countries:
        currency = currency_for_country(merchant)
        available_controls = [b for b in controls if b in buyer_countries and b != merchant]
        control_buyers = [merchant, *available_controls]
        plan.extend(
            build_diagnostic_plan(
                run_id=run_id,
                merchant_country=merchant,
                diagnostic_amounts=amounts,
                control_buyers=control_buyers,
                currency=currency,
                adapter=adapter,
                max_new_captures=max_new_captures,
            )
        )
    return plan


def _tag_public_rate_validation(plan: list[Case], registry: dict[str, Any]) -> None:
    """Tag planned cases as public-rate validation and record planning-time registry status."""
    for case in plan:
        case.execution_classification = "public_rate_validation"
        entry = registry.get(case.merchant_country, {})
        case.planning_time_registry_status = entry.get("status")


def _tag_diagnostic_sandbox_pricing(plan: list[Case], registry: dict[str, Any]) -> None:
    """Tag planned cases as diagnostic sandbox-pricing observations."""
    for case in plan:
        case.execution_classification = "diagnostic_sandbox_pricing"
        entry = registry.get(case.merchant_country, {})
        case.planning_time_registry_status = entry.get("status")
        case.pilot_metadata["diagnostic_sandbox_pricing"] = True
        observed = (entry.get("manual_send_observation") or {}).get("observed_account_formula", {})
        if observed.get("percentage"):
            case.pilot_metadata["account_specific_base_rate"] = (
                f"{observed['percentage']}% + {observed['fixed']['currency']} {observed['fixed']['value']}"
            )


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
