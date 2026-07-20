"""Orchestration for the PayPal Sandbox manual Send Money validation path."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from paypal_sandbox_validation.accounts import Account, parse_accounts_csv
from paypal_sandbox_validation.diagnostics import validate_case_constraints
from paypal_sandbox_validation.manual_browser import ManualPaymentBrowser
from paypal_sandbox_validation.models import Case, CaseStatus, ReconciliationStatus
from paypal_sandbox_validation.nvp import (
    PayPalNVPClient,
    extract_transaction_details,
    poll_for_unique_transaction,
)
from paypal_sandbox_validation.persistence import (
    load_manual_case,
    load_manual_plan,
    load_manual_private_state,
    save_manual_case,
    save_manual_private_state,
    save_manual_results,
)
from paypal_sandbox_validation.quote_adapter import QuoteAdapter, QuoteResolutionError
from paypal_sandbox_validation.reconciliation import reconcile


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_paypal_evidence(details: dict[str, Any]) -> dict[str, Any] | None:
    """Convert NVP GetTransactionDetails into the evidence shape used by reconcile."""
    if not details:
        return None
    amt = details.get("amt")
    fee = details.get("fee_amt")
    currency = details.get("currency_code")
    if not amt or not fee or not currency:
        return None

    # NVP may return the fee as a negative number in TransactionSearch and as a
    # positive number in GetTransactionDetails. Use the absolute value.
    try:
        fee_value = str(abs(Decimal(str(fee))))
    except Exception:
        fee_value = str(fee)

    try:
        gross_value = Decimal(str(amt))
        net_value = gross_value - Decimal(fee_value)
    except Exception:
        return None

    payment_status = str(details.get("payment_status", "")).upper()
    status = "COMPLETED" if payment_status == "COMPLETED" else details.get("payment_status", "")
    payer_country = details.get("country_code", "")

    return {
        "status": status,
        "transaction_type": details.get("transaction_type"),
        "payment_type": details.get("payment_type"),
        "gross_amount": {"value": str(gross_value), "currency_code": currency},
        "paypal_fee": {"value": fee_value, "currency_code": currency},
        "net_amount": {"value": str(net_value), "currency_code": currency},
        "payer_country": payer_country,
    }


def _detect_fx(details: dict[str, Any]) -> bool:
    """Return True if the NVP details indicate currency conversion."""
    if details.get("exchange_rate"):
        return True
    settle_amt = details.get("settle_amt")
    settle_currency = details.get("settle_currency")
    return bool(
        settle_amt and settle_currency and settle_currency != details.get("currency_code")
    )


def _resolve_manual_quote(
    adapter: QuoteAdapter,
    case: Case,
    actual_fee: Decimal,
) -> dict[str, Any] | None:
    """Resolve the capability that matches the observed fee, preferring other_commercial.

    Expected conceptual product is ``other_commercial / standard``. If that does not
    match the observed fee, ``goods_and_services / standard`` is tried. If neither
    matches, the other_commercial quote is returned so the mismatch is reported.
    """
    candidates = [
        ("other_commercial", "standard"),
        ("goods_and_services", "standard"),
    ]
    fallback: dict[str, Any] | None = None
    fallback_product: str | None = None
    fallback_variant: str | None = None

    for product_id, variant_id in candidates:
        try:
            quote = adapter.build_quote(
                case.merchant_country,
                case.buyer_country,
                case.amount,
                case.currency,
                product_id=product_id,
                variant_id=variant_id,
            )
        except QuoteResolutionError:
            continue
        fee_value = quote.get("processing_fee", {}).get("value")
        try:
            if fee_value is not None and Decimal(str(fee_value)) == actual_fee:
                case.product_id = product_id
                case.variant_id = variant_id
                return quote
        except Exception:
            pass
        if fallback is None:
            fallback = quote
            fallback_product = product_id
            fallback_variant = variant_id

    if fallback is None:
        return None

    case.product_id = fallback_product or "other_commercial"
    case.variant_id = fallback_variant or "standard"
    return fallback


def _quote_for_case(
    adapter: QuoteAdapter,
    case: Case,
    product_id: str | None = None,
    variant_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        if product_id and variant_id:
            return adapter.build_quote(
                case.merchant_country,
                case.buyer_country,
                case.amount,
                case.currency,
                product_id=product_id,
                variant_id=variant_id,
            )
        return adapter.build_quote_manual(
            case.merchant_country,
            case.buyer_country,
            case.amount,
            case.currency,
        )
    except QuoteResolutionError as exc:
        case.paypal_error = {"message": str(exc), "status": exc.status}
        case.status = CaseStatus.FAILED
        return None


def _case_id(run_id: str, merchant_country: str, buyer_country: str, amount: str, currency: str) -> str:
    return f"manual-{merchant_country}-{buyer_country}-{amount}-{currency}-{run_id[:8]}"


def build_manual_plan(
    run_id: str,
    profile: str,
    accounts_csv: str,
    cases: list[tuple[str, str, str, str]],
) -> list[Case]:
    """Build a manual Send Money plan from explicit cases."""
    accounts = parse_accounts_csv(accounts_csv)
    merchants = {a.country_code: a for a in accounts if a.is_business()}
    buyers = {a.country_code: a for a in accounts if a.is_personal()}
    adapter = QuoteAdapter()

    plan: list[Case] = []
    for merchant_country, buyer_country, amount, currency in cases:
        merchant = merchants.get(merchant_country.upper())
        buyer = buyers.get(buyer_country.upper())
        if not merchant or not buyer:
            continue

        case_id = _case_id(run_id, merchant_country, buyer_country, amount, currency)
        case = Case(
            case_id=case_id,
            run_id=run_id,
            merchant_country=merchant_country.upper(),
            buyer_country=buyer_country.upper(),
            amount=amount,
            currency=currency,
            execution_path="manual_send_to_business",
            product_id="other_commercial",
            variant_id="standard",
            status=CaseStatus.PLANNED,
        )
        # Pre-compute a prediction quote; actual product is resolved from the fee.
        quote = _quote_for_case(adapter, case, product_id="other_commercial", variant_id="standard")
        if not quote:
            case.status = CaseStatus.FAILED
            case.paypal_issue = ReconciliationStatus.LIBRARY_NOT_CALCULABLE.value
        case.quote = quote
        plan.append(case)
    return plan


def _search_time_window(submitted_at: datetime) -> tuple[str, str]:
    # Allow a few minutes of sandbox clock drift around the submission time.
    start = submitted_at - timedelta(minutes=2)
    end = submitted_at + timedelta(minutes=5)
    return _format_utc(start), _format_utc(end)


def _lookup_transaction(
    case: Case,
    merchant: Account,
    buyer: Account,
    submitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Search NVP for an existing transaction by amount/currency/buyer and time window."""
    if submitted_at is None and case.manual_submitted_at:
        try:
            submitted_at = datetime.fromisoformat(case.manual_submitted_at)
        except Exception:
            submitted_at = None

    if submitted_at is None:
        # No submission timestamp known; search a broad recent window.
        start = _format_utc(datetime.now(UTC) - timedelta(hours=24))
        end = _format_utc(datetime.now(UTC))
    else:
        start, end = _search_time_window(submitted_at)

    with PayPalNVPClient(merchant) as client:
        search_result = poll_for_unique_transaction(
            client,
            start_date=start,
            end_date=end,
            amount=case.amount,
            currency=case.currency,
            buyer_email=buyer.primary_email_alias,
            max_attempts=6,
            delay_seconds=5.0,
        )
    if search_result.get("status") != "found":
        return search_result

    transaction = search_result["transaction"]
    details_result = _fetch_details(case, merchant, transaction["transaction_id"])
    if details_result.get("status") != "found":
        return details_result
    return {
        "status": "found",
        "transaction": transaction,
        "details": details_result["details"],
    }


def _fetch_details(
    case: Case,
    merchant: Account,
    transaction_id: str,
) -> dict[str, Any]:
    """Fetch GetTransactionDetails and convert to a secret-free evidence dict."""
    with PayPalNVPClient(merchant) as client:
        response = client.get_transaction_details(transaction_id)
    if not response.is_success():
        return {
            "status": "nvp_search_failed",
            "error": response.error_message() or "GetTransactionDetails failed",
        }
    details = extract_transaction_details(response)
    if details is None:
        return {"status": "nvp_search_failed", "error": "Could not parse transaction details"}
    return {"status": "found", "details": details}


def _run_reconciliation(
    case: Case,
    details: dict[str, Any],
    adapter: QuoteAdapter,
) -> Case:
    """Reconcile NVP details with the payment-fee library."""
    case.evidence_source = "nvp_get_transaction_details"

    if _detect_fx(details):
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.EXCLUDED_FX_CASE.value
        case.paypal_error = {"message": "Currency conversion or settle-currency mismatch detected"}
        case.manual_state = "failed"
        return case

    try:
        actual_fee = Decimal(str(details.get("fee_amt", "0"))).copy_abs()
    except Exception:
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE.value
        case.paypal_error = {"message": "FEEAMT missing or not numeric"}
        case.manual_state = "failed"
        return case

    evidence = _build_paypal_evidence(details)
    if not evidence:
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE.value
        case.paypal_error = {"message": "Could not build evidence from NVP details"}
        case.manual_state = "failed"
        return case

    case.paypal_evidence = evidence

    quote = _resolve_manual_quote(adapter, case, actual_fee)
    if not quote:
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.LIBRARY_NOT_CALCULABLE.value
        case.paypal_error = {"message": "No calculable product for manual path"}
        case.manual_state = "failed"
        return case
    case.quote = quote

    rec = reconcile(
        paypal_evidence=evidence,
        quote=quote,
        merchant_country=case.merchant_country,
        buyer_country=case.buyer_country,
        observed_payer_country=evidence.get("payer_country"),
    )
    case.reconciliation = rec.model_dump(exclude_none=True)
    case.status = CaseStatus.RECONCILED if rec.status == ReconciliationStatus.MATCH else CaseStatus.FAILED

    validation = validate_case_constraints(case)
    if not validation["valid"]:
        case.status = CaseStatus.FAILED
        case.paypal_issue = validation["classification"]

    case.manual_state = "reconciled" if case.status == CaseStatus.RECONCILED else "failed"
    return case


def run_manual_case(
    case: Case,
    buyer: Account,
    merchant: Account,
    adapter: QuoteAdapter,
    browser: ManualPaymentBrowser,
    headless: bool = True,
) -> Case:
    """Execute (or resume) one manual Send Money case using NVP for verification."""
    if case.status in {CaseStatus.RECONCILED, CaseStatus.FAILED}:
        return case

    reference = f"payment-fee-validation:{case.run_id}:{case.case_id}"
    private_state = load_manual_private_state(case.run_id)
    transaction_id = private_state.get(case.case_id)

    # Resume path 1: a transaction ID was already captured for this case.
    if transaction_id:
        result = _fetch_details(case, merchant, transaction_id)
        if result.get("status") == "found":
            case.nvp_transaction_id = transaction_id
            case.pilot_metadata["duplicate_prevention"] = "resumed_from_private_state"
            case.status = CaseStatus.MERCHANT_TRANSACTION_FOUND
            case.manual_state = "merchant_transaction_found"
            return _run_reconciliation(case, result["details"], adapter)

    # Resume path 2: the payment was submitted but the transaction ID was not yet saved.
    if case.status == CaseStatus.PAYMENT_SUBMITTED and case.manual_submitted_at:
        submitted_at: datetime | None = None
        with contextlib.suppress(Exception):
            submitted_at = datetime.fromisoformat(case.manual_submitted_at)
        if submitted_at is not None:
            result = _lookup_transaction(case, merchant, buyer, submitted_at)
            if result.get("status") == "found":
                tx_id = result["transaction"]["transaction_id"]
                case.nvp_transaction_id = tx_id
                case.pilot_metadata["duplicate_prevention"] = "resumed_from_submitted_at"
                save_manual_private_state(case.run_id, case.case_id, tx_id)
                case.status = CaseStatus.MERCHANT_TRANSACTION_FOUND
                case.manual_state = "merchant_transaction_found"
                return _run_reconciliation(case, result["details"], adapter)

    # Fresh path: perform the buyer-side Playwright send.
    if case.status == CaseStatus.PLANNED:
        result = browser.send_payment(
            buyer=buyer,
            merchant=merchant,
            amount=case.amount,
            currency=case.currency,
            note=reference,
        )

        if result.get("status") != "submitted":
            case.status = CaseStatus.FAILED
            case.paypal_issue = result.get("status", ReconciliationStatus.UNSUPPORTED_PAYPAL_UI_STATE.value)
            case.paypal_error = {"message": result.get("error", "")}
            case.manual_state = "failed"
            return case

        case.buyer_ui_evidence = {k: v for k, v in result.items() if k not in {"status", "success", "error"}}
        case.manual_payment_type = result.get("payment_type_selected", "unknown")
        case.funding_source = result.get("funding_source", "unknown")
        case.status = CaseStatus.PAYMENT_SUBMITTED
        case.manual_state = "payment_submitted"
        case.manual_submitted_at = result.get("submitted_at")
        save_manual_case(case.run_id, case)

    # Locate the merchant transaction with NVP.
    submitted_at: datetime | None = None
    if case.manual_submitted_at:
        with contextlib.suppress(Exception):
            submitted_at = datetime.fromisoformat(case.manual_submitted_at)
    if submitted_at is None:
        submitted_at = datetime.now(UTC)

    result = _lookup_transaction(case, merchant, buyer, submitted_at)
    if result.get("status") != "found":
        case.pilot_metadata["duplicate_prevention"] = result.get("status", "no_unique_match")
        case.status = CaseStatus.FAILED
        case.paypal_issue = result.get("status", ReconciliationStatus.MERCHANT_TRANSACTION_NOT_FOUND.value)
        case.paypal_error = {"message": result.get("error", "")}
        case.manual_state = "failed"
        return case

    tx_id = result["transaction"]["transaction_id"]
    case.nvp_transaction_id = tx_id
    save_manual_private_state(case.run_id, case.case_id, tx_id)
    case.pilot_metadata["duplicate_prevention"] = "unique_nvp_match"
    case.status = CaseStatus.MERCHANT_TRANSACTION_FOUND
    case.manual_state = "merchant_transaction_found"

    return _run_reconciliation(case, result["details"], adapter)


def run_manual_plan(
    run_id: str,
    accounts_csv: str,
    stop_after_first_mismatch: bool = True,
    headless: bool = True,
    slow_mo: int = 0,
) -> dict[str, Any]:
    """Run all cases in a manual plan, resuming where possible."""
    plan = load_manual_plan(run_id)
    accounts = parse_accounts_csv(accounts_csv)
    merchants = {a.country_code: a for a in accounts if a.is_business()}
    buyers = {a.country_code: a for a in accounts if a.is_personal()}
    adapter = QuoteAdapter()
    results: list[dict[str, Any]] = []

    with ManualPaymentBrowser(headless=headless, slow_mo=slow_mo) as browser:
        for case in plan:
            # Resume from the most recently persisted case state.
            with contextlib.suppress(Exception):
                case = load_manual_case(run_id, case.case_id)

            if case.status not in {CaseStatus.RECONCILED, CaseStatus.FAILED}:
                merchant = merchants.get(case.merchant_country)
                buyer = buyers.get(case.buyer_country)
                if not merchant or not buyer:
                    case.status = CaseStatus.FAILED
                    case.paypal_error = {"message": "Missing account"}
                else:
                    case = run_manual_case(case, buyer, merchant, adapter, browser)
                save_manual_case(run_id, case)

            results.append(case.model_dump())

            rec = case.reconciliation or {}
            if rec.get("status") in {
                "fee_mismatch",
                "net_amount_mismatch",
                "currency_mismatch",
                "buyer_country_mismatch",
            } and stop_after_first_mismatch:
                break

    results_dict = {"run_id": run_id, "cases": results}
    save_manual_results(run_id, results_dict)
    return results_dict
