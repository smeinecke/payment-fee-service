from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from paypal_sandbox_validation.approval import approve_order
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.models import (
    Account,
    Case,
    CaseStatus,
    ReconciliationStatus,
    RunConfig,
)
from paypal_sandbox_validation.oauth import OAuthCache, OAuthError, OAuthProbeStatus, fetch_token
from paypal_sandbox_validation.paypal_api import (
    PayPalAPIError,
    PayPalClient,
    build_order_payload,
    extract_approval_url,
)
from paypal_sandbox_validation.persistence import (
    load_results,
    save_case,
    save_configuration_summary,
    save_json,
    save_results,
    save_sanitized_order,
)
from paypal_sandbox_validation.planner import (
    generate_request_id,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter, QuoteResolutionError
from paypal_sandbox_validation.reconciliation import reconcile
from paypal_sandbox_validation.redaction import mask_value, redact_path, sanitize_dict
from paypal_sandbox_validation.reporting import build_summary, save_junit, save_summary, save_summary_markdown


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
    case.paypal_operations_executed_in_current_run = existing_case.paypal_operations_executed_in_current_run or 0
    case.observation_source = existing_case.observation_source or case.observation_source
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
    case.pilot_metadata = {**case.pilot_metadata, **existing_case.pilot_metadata}
    case.paypal_operation = existing_case.paypal_operation or case.paypal_operation
    case.paypal_debug_id = existing_case.paypal_debug_id or case.paypal_debug_id
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
                ReconciliationStatus.FEE_MISMATCH,
                ReconciliationStatus.NET_AMOUNT_MISMATCH,
                ReconciliationStatus.CURRENCY_MISMATCH,
                ReconciliationStatus.BUYER_COUNTRY_MISMATCH,
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
    if not merchant or buyer is None:
        case.status = CaseStatus.FAILED
        return _case_dict(case, error="Missing merchant or buyer account")

    # Fixture-hydrated cases are already reconciled; do not start a callback server
    # or call PayPal again.
    if case.status == CaseStatus.RECONCILED:
        return _case_dict(case)

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
        case.paypal_operations_executed_in_current_run += 1
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
    case.paypal_operations_executed_in_current_run += 1
    case.observation_source = "live_capture"
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
