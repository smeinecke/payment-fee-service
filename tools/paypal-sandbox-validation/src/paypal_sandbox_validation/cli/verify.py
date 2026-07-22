from __future__ import annotations

import json
import sys

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
)
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.configuration import (
    currency_for_country,
)
from paypal_sandbox_validation.oauth import OAuthCache
from paypal_sandbox_validation.paypal_api import (
    order_payload_signature,
)
from paypal_sandbox_validation.persistence import (
    artifact_root,
    save_json,
)
from paypal_sandbox_validation.planner import (
    generate_run_id,
)

from . import _env_csv_default, cli
from .diagnose import _create_and_associate_order, _order_payee_association


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

    currency = currency or currency_for_country(merchant_account.country_code)
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
