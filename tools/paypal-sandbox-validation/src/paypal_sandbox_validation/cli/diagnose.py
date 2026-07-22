from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.accounts import (
    parse_accounts_csv,
)
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.models import (
    Account,
    Case,
    ReconciliationStatus,
    RunConfig,
)
from paypal_sandbox_validation.oauth import OAuthCache, OAuthError, fetch_token
from paypal_sandbox_validation.paypal_api import (
    PayPalAPIError,
    PayPalClient,
    build_order_payload,
    extract_payee_info,
    extract_paypal_error_fields,
)
from paypal_sandbox_validation.persistence import (
    save_plan,
)
from paypal_sandbox_validation.planner import (
    generate_request_id,
    generate_run_id,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter
from paypal_sandbox_validation.redaction import sanitize_paypal_order

from . import _env_csv_default, cli
from .runner import _execute_plan


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
        "result": (
            ReconciliationStatus.MATCH
            if root_cause.get("category") != "sandbox_account_configuration"
            else ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE
        ),
        "diagnostic_run_id": output_dir.parent.name,
    }
    fixture_path = fixture_dir / f"{original.run_id}-{original.case_id}.json"
    fixture_path.write_text(json.dumps(fixture, indent=2, sort_keys=True))
