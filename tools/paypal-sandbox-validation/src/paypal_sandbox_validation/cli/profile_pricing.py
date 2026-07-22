from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
)
from paypal_sandbox_validation.profile_pricing import (
    ProfilePricingBrowser,
    record_profile_pricing,
    validate_profile_pricing_input,
)
from paypal_sandbox_validation.qualification import (
    default_qualification_path,
    load_qualification_registry,
    save_qualification_registry,
)

from . import _env_csv_default, cli


@cli.command("record-profile-pricing")
@click.option("--merchant", required=True, help="Merchant country code.")
@click.option("--wallet-percentage", required=True, help="Wallet pricing percentage, e.g. 1.90")
@click.option("--wallet-fixed", required=True, help="Wallet fixed fee value, e.g. 0.35")
@click.option("--wallet-currency", required=True, help="Wallet fixed fee currency, e.g. EUR")
@click.option("--card-percentage", required=True, help="Card pricing percentage, e.g. 2.99")
@click.option("--card-fixed", required=True, help="Card fixed fee value, e.g. 0.39")
@click.option("--card-currency", required=True, help="Card fixed fee currency, e.g. EUR")
@click.option("--card-qualifier", default=None, help="Card rate qualifier, e.g. starting_at")
@click.option("--observed-at", default=None, help="ISO-8601 timestamp of observation.")
@click.option("--screenshot-sha256", default=None, help="Optional SHA-256 of a screenshot.")
@click.option("--manual-send-to-business", is_flag=True, help="Mark manual-send path confirmed by profile pricing.")
@click.option("--orders-v2-checkout", is_flag=True, help="Mark Orders-v2 checkout path confirmed by profile pricing.")
@click.option("--accounts-csv", type=click.Path(exists=True, dir_okay=False), default=_env_csv_default)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def record_profile_pricing_cmd(
    merchant: str,
    wallet_percentage: str,
    wallet_fixed: str,
    wallet_currency: str,
    card_percentage: str,
    card_fixed: str,
    card_currency: str,
    card_qualifier: str | None,
    observed_at: str | None,
    screenshot_sha256: str | None,
    manual_send_to_business: bool,
    orders_v2_checkout: bool,
    accounts_csv: str,
    qualification_registry: str,
) -> None:
    """Record manually observed Sandbox Business profile pricing without executing a payment."""
    accounts = parse_accounts_csv(accounts_csv)
    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    if merchant.upper() not in merchant_accounts:
        click.echo(f"Business account for merchant {merchant} not found in {accounts_csv}", err=True)
        sys.exit(1)

    evidence = validate_profile_pricing_input(
        merchant_country=merchant.upper(),
        wallet_percentage=wallet_percentage,
        wallet_fixed=wallet_fixed,
        wallet_currency=wallet_currency,
        card_percentage=card_percentage,
        card_fixed=card_fixed,
        card_currency=card_currency,
        card_qualifier=card_qualifier,
    )
    if screenshot_sha256:
        evidence.screenshot_sha256 = screenshot_sha256

    registry = load_qualification_registry(Path(qualification_registry))
    record_profile_pricing(
        registry,
        evidence,
        set_manual_send=manual_send_to_business,
        set_orders_v2=orders_v2_checkout,
        observed_at=observed_at,
    )
    save_qualification_registry(registry, Path(qualification_registry))
    click.echo(json.dumps(registry[merchant.upper()], indent=2))


@cli.command("inspect-profile-pricing")
@click.option("--merchant", required=True, help="Merchant country code.")
@click.option("--accounts-csv", type=click.Path(exists=True, dir_okay=False), default=_env_csv_default)
@click.option("--headful", is_flag=True)
@click.option("--headed", is_flag=True)
@click.option("--slow-mo", type=int, default=0)
@click.option(
    "--qualification-registry",
    type=click.Path(dir_okay=False),
    default=default_qualification_path,
)
def inspect_profile_pricing_cmd(
    merchant: str,
    accounts_csv: str,
    headful: bool,
    headed: bool,
    slow_mo: int,
    qualification_registry: str,
) -> None:
    """Read the authenticated Sandbox Business profile pricing page (read-only, no payment)."""
    accounts = parse_accounts_csv(accounts_csv)
    merchant_accounts = {a.country_code: a for a in accounts if a.is_business()}
    account = merchant_accounts.get(merchant.upper())
    if not account:
        click.echo(f"Business account for merchant {merchant} not found in {accounts_csv}", err=True)
        sys.exit(1)

    from paypal_sandbox_validation.accounts import COUNTRY_CURRENCY

    wallet_currency = COUNTRY_CURRENCY.get(merchant.upper(), "")
    card_currency = wallet_currency
    if not wallet_currency:
        click.echo(f"Could not determine currency for merchant {merchant}", err=True)
        sys.exit(1)

    headless = not (headful or headed)
    with ProfilePricingBrowser(headless=headless, slow_mo=slow_mo) as browser:
        evidence = browser.inspect(account, merchant.upper(), wallet_currency, card_currency)

    registry = load_qualification_registry(Path(qualification_registry))
    record_profile_pricing(registry, evidence)
    save_qualification_registry(registry, Path(qualification_registry))
    click.echo(json.dumps(registry[merchant.upper()], indent=2))
