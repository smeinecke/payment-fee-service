from __future__ import annotations

import json
import sys
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
)
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.configuration import (
    currency_for_country,
)
from paypal_sandbox_validation.models import (
    Case,
    CaseStatus,
    ReconciliationStatus,
)
from paypal_sandbox_validation.oauth import OAuthCache, fetch_token
from paypal_sandbox_validation.paypal_api import (
    PayPalAPIError,
    PayPalClient,
    extract_approval_url,
    extract_paypal_error_fields,
    order_payload_signature,
)
from paypal_sandbox_validation.persistence import (
    artifact_root,
    save_json,
)
from paypal_sandbox_validation.planner import (
    generate_request_id,
    generate_run_id,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.redaction import sanitize_paypal_order

from . import _env_csv_default, cli
from .diagnose import _create_and_associate_order


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

    currency = currency or currency_for_country(merchant_account.country_code)
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
        validate_case_constraints(case)
        if quote
        else {"valid": False, "classification": ReconciliationStatus.LIBRARY_NOT_CALCULABLE.value}
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
