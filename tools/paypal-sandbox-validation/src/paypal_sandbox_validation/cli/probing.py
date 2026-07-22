from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
    validate_accounts,
)
from paypal_sandbox_validation.configuration import (
    validate_configuration,
)
from paypal_sandbox_validation.nvp import PayPalNVPClient
from paypal_sandbox_validation.oauth import probe_credentials
from paypal_sandbox_validation.persistence import (
    save_json,
)
from paypal_sandbox_validation.planner import (
    generate_run_id,
)

from . import _env_csv_default, cli


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
