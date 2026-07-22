"""Orchestration for the PayPal Sandbox manual Send Money validation path."""

from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from paypal_sandbox_validation.accounts import Account, parse_accounts_csv
from paypal_sandbox_validation.configuration import get_manual_send_scenario
from paypal_sandbox_validation.diagnostics import infer_formula as _infer_formula
from paypal_sandbox_validation.diagnostics import validate_case_constraints
from paypal_sandbox_validation.manual_browser import ManualPaymentBrowser
from paypal_sandbox_validation.models import Case, CaseStatus, ReconciliationStatus
from paypal_sandbox_validation.numeric import _decimal
from paypal_sandbox_validation.nvp import (
    PayPalNVPClient,
    extract_transaction_details,
    poll_for_unique_transaction,
)
from paypal_sandbox_validation.persistence import (
    load_json,
    load_manual_case,
    load_manual_plan,
    load_manual_private_state,
    manual_artifact_root,
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
    return bool(settle_amt and settle_currency and settle_currency != details.get("currency_code"))


def _prediction_record(case: Case, quote: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of a quote that must remain stable after submission."""
    meta = quote.get("_schedule_metadata") or {}
    data = quote.get("data") or {}
    return {
        "product_id": case.product_id,
        "variant_id": case.variant_id,
        "base_rule_id": meta.get("base_rule_id"),
        "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
        "payer_region": meta.get("payer_region"),
        "base_percentage": meta.get("base_percentage"),
        "fixed_amount": meta.get("fixed_amount"),
        "surcharge_percentage": meta.get("surcharge_percentage"),
        "predicted_total_fee": quote.get("processing_fee", {}).get("value"),
        "predicted_net_amount": quote.get("net_amount", {}).get("value"),
        "data_revision": data.get("content_sha256"),
    }


def _sha256(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _set_prediction(case: Case, quote: dict[str, Any]) -> None:
    """Persist the pre-submission prediction and its integrity hash."""
    case.quote = quote
    case.product_selection_source = "explicit_execution_path_mapping"
    case.prediction_provenance = "pre_submission_prediction"
    case.prediction_created_before_original_submission = True
    case.prediction_created_before_observation_reuse = True
    case.original_submission_timestamp_known = False
    case.prediction_created_at = datetime.now(UTC).isoformat()
    case.prediction_sha256 = _sha256(_prediction_record(case, quote))


def _set_historical_requoted_prediction(
    case: Case,
    original_submitted_at: str | None,
) -> None:
    """Mark a reused historical observation as re-quoted at reuse time.

    The prediction record is created now, before the reused NVP observation is
    reconciled, but it is not before the original submission.
    """
    case.prediction_provenance = "historical_observation_requoted"
    case.prediction_created_before_original_submission = False
    case.prediction_created_before_observation_reuse = True
    case.original_submission_timestamp_known = original_submitted_at is not None


def _verify_prediction_unchanged(case: Case) -> bool:
    """Return True if the stored prediction hash still matches the quote."""
    if not case.quote or not case.prediction_sha256:
        return False
    return _sha256(_prediction_record(case, case.quote)) == case.prediction_sha256


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
    """Build a manual Send Money plan from explicit cases using a fixed execution-path mapping."""
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
        product_id = "goods_and_services"
        variant_id = "standard"
        status = CaseStatus.PLANNED
        paypal_issue: str | None = None
        paypal_error: dict[str, Any] | None = None
        quote: dict[str, Any] | None = None

        try:
            scenario = adapter.resolve_manual_scenario(merchant_country.upper())
            product_id = scenario["product_id"]
            variant_id = scenario["variant_id"]
            quote = adapter.build_quote(
                merchant_country=merchant_country.upper(),
                buyer_country=buyer_country.upper(),
                amount=amount,
                currency=currency,
                product_id=product_id,
                variant_id=variant_id,
            )
            status = CaseStatus.PREDICTION_READY
        except QuoteResolutionError as exc:
            status = CaseStatus.FAILED
            paypal_issue = ReconciliationStatus.ACCOUNT_CAPABILITY_UNAVAILABLE.value
            paypal_error = {"message": str(exc), "status": exc.status}

        case = Case(
            case_id=case_id,
            run_id=run_id,
            merchant_country=merchant_country.upper(),
            buyer_country=buyer_country.upper(),
            amount=amount,
            currency=currency,
            execution_path="manual_send_to_business",
            product_id=product_id,
            variant_id=variant_id,
            status=status,
        )
        if quote is not None:
            _set_prediction(case, quote)
        if paypal_issue:
            case.paypal_issue = paypal_issue
        if paypal_error:
            case.paypal_error = paypal_error

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


def _find_existing_transaction(
    case: Case,
    merchant: Account,
    buyer: Account,
) -> dict[str, Any]:
    """Look for a previously reconciled manual-send transaction to reuse.

    Reuse is allowed only when a persisted case with the same execution path,
    product and variant reconciled with zero delta, and the corresponding
    private transaction ID is available. This prevents the observed PayPal fee
    from influencing product selection or reusing an incompatible transaction.
    """
    candidate = _find_persisted_reusable_case(case)
    if not candidate:
        return {"status": "nvp_transaction_not_found"}

    tx_id = candidate["transaction_id"]
    _set_historical_requoted_prediction(case, candidate.get("manual_submitted_at"))

    details_result = _fetch_details(case, merchant, tx_id)
    if details_result.get("status") != "found":
        return details_result

    scenario = get_manual_send_scenario(adapter_scenarios(merchant.country_code), case.merchant_country)
    if _validate_nvp_transaction(case, details_result["details"], scenario) is not None:
        return {"status": "nvp_transaction_not_found"}

    return {
        "status": "found",
        "transaction": {
            "transaction_id": tx_id,
            "timestamp": candidate.get("manual_submitted_at"),
        },
        "details": details_result["details"],
    }


def _find_persisted_reusable_case(case: Case) -> dict[str, Any] | None:
    """Search persisted manual run artifacts for a matching reconciled case."""
    root = manual_artifact_root()
    candidates: list[tuple[float, dict[str, Any]]] = []

    for run_dir in root.iterdir():
        if not run_dir.is_dir() or run_dir.name == case.run_id:
            continue
        case_file = run_dir / "cases" / f"{case.case_id}.json"
        private_file = run_dir / "private" / "nvp-private.json"
        if not case_file.exists() or not private_file.exists():
            continue
        try:
            private_state = load_json(private_file)
            old = Case.model_validate(load_json(case_file))
        except Exception:
            continue

        tx_id = private_state.get(case.case_id)
        if not tx_id:
            continue
        if old.status != CaseStatus.RECONCILED:
            continue
        if old.product_id != case.product_id or old.variant_id != case.variant_id:
            continue
        if old.merchant_country != case.merchant_country or old.buyer_country != case.buyer_country:
            continue
        if old.amount != case.amount or old.currency != case.currency:
            continue
        if (old.reconciliation or {}).get("status") != ReconciliationStatus.MATCH:
            continue
        if (old.reconciliation or {}).get("delta_minor_units") != 0:
            continue

        mtime = case_file.stat().st_mtime
        candidates.append((mtime, {"transaction_id": tx_id, "manual_submitted_at": old.manual_submitted_at}))

    if not candidates:
        return None
    # Use the most recent matching persisted case.
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def adapter_scenarios(_country: str) -> dict[str, Any]:
    """Load the shared scenarios configuration.

    The country argument is reserved for future market-specific mapping files.
    """
    from paypal_sandbox_validation.configuration import load_scenarios

    return load_scenarios()


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


def _validate_nvp_transaction(
    case: Case,
    details: dict[str, Any],
    scenario: dict[str, Any] | None,
) -> str | None:
    """Return a ReconciliationStatus value if NVP semantics are invalid, else None."""
    expected_type = (scenario or {}).get("expected_nvp_transaction_type", "sendmoney")
    supported_payment_types = (scenario or {}).get("supported_nvp_payment_types", ["instant"])

    if details.get("transaction_type") != expected_type:
        return ReconciliationStatus.TRANSACTION_TYPE_MISMATCH.value

    payment_type = details.get("payment_type")
    if payment_type not in supported_payment_types:
        return ReconciliationStatus.UNSUPPORTED_PAYMENT_TYPE.value

    if str(details.get("payment_status", "")).upper() != "COMPLETED":
        return ReconciliationStatus.INCOMPLETE_PAYMENT.value

    observed_country = details.get("country_code", "")
    if observed_country and observed_country.upper() != case.buyer_country.upper():
        return ReconciliationStatus.BUYER_COUNTRY_MISMATCH.value

    return None


def _run_reconciliation(
    case: Case,
    details: dict[str, Any],
    adapter: QuoteAdapter,
) -> Case:
    """Reconcile NVP details with the preselected payment-fee library prediction."""
    case.evidence_source = "nvp_get_transaction_details"

    if case.manual_submitted_at:
        case.original_submission_timestamp_known = True

    if _detect_fx(details):
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.EXCLUDED_FX_CASE.value
        case.paypal_error = {"message": "Currency conversion or settle-currency mismatch detected"}
        case.manual_state = "failed"
        return case

    try:
        Decimal(str(details.get("fee_amt", "0"))).copy_abs()
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

    scenario = get_manual_send_scenario(adapter.scenarios, case.merchant_country)
    semantic_issue = _validate_nvp_transaction(case, details, scenario)
    if semantic_issue:
        case.status = CaseStatus.FAILED
        case.paypal_issue = semantic_issue
        case.paypal_error = {"message": "NVP transaction semantics do not match the configured scenario"}
        case.manual_state = "failed"
        return case

    if not _verify_prediction_unchanged(case):
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.PREDICTION_CHANGED.value
        case.paypal_error = {"message": "Prediction record changed after observation"}
        case.manual_state = "failed"
        return case
    case.prediction_unchanged_after_observation = True

    if not case.quote:
        case.status = CaseStatus.FAILED
        case.paypal_issue = ReconciliationStatus.LIBRARY_NOT_CALCULABLE.value
        case.paypal_error = {"message": "No preselected calculable quote for manual path"}
        case.manual_state = "failed"
        return case

    rec = reconcile(
        paypal_evidence=evidence,
        quote=case.quote,
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
        case.paypal_error = {"message": validation.get("classification", "Case constraint validation failed")}

    if case.status == CaseStatus.FAILED and not case.paypal_issue:
        case.paypal_issue = rec.status.value
        case.paypal_error = {"message": rec.root_cause or "Reconciliation failed"}

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

    # Fresh path: check for an existing reusable transaction before Playwright.
    if case.status in {CaseStatus.PLANNED, CaseStatus.PREDICTION_READY}:
        existing = _find_existing_transaction(case, merchant, buyer)
        if existing.get("status") == "found":
            tx_id = existing["transaction"]["transaction_id"]
            case.nvp_transaction_id = tx_id
            case.pilot_metadata["duplicate_prevention"] = "existing_transaction_reused"
            save_manual_private_state(case.run_id, case.case_id, tx_id)
            case.manual_submitted_at = existing["details"].get("order_time")
            case.status = CaseStatus.MERCHANT_TRANSACTION_FOUND
            case.manual_state = "merchant_transaction_found"
            return _run_reconciliation(case, existing["details"], adapter)

        # No existing transaction; perform the buyer-side Playwright send.
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


def _eligible_single_scenario_cases(cases: list[Case]) -> list[Case]:
    """Return cases that share the same product/variant and have both evidence and a quote."""
    eligible = [c for c in cases if c.paypal_evidence and c.quote]
    if not eligible:
        return []
    product_ids = {c.product_id for c in eligible}
    variant_ids = {c.variant_id for c in eligible}
    if len(product_ids) != 1 or len(variant_ids) != 1:
        return []
    return eligible


def _collect_gross_fee_pairs(eligible: list[Case]) -> list[dict[str, Any]]:
    """Convert eligible cases into the canonical observation dict shape used by ``infer_formula``."""
    observations: list[dict[str, Any]] = []
    for c in eligible:
        ev = c.paypal_evidence or {}
        q = c.quote or {}
        gross = _decimal(ev.get("gross_amount", {}).get("value"))
        fee = _decimal(ev.get("paypal_fee", {}).get("value"))
        if gross is None or fee is None:
            continue
        observations.append(
            {
                "amount": str(gross),
                "currency": c.currency,
                "paypal_fee": str(fee),
                "buyer_country": c.buyer_country,
                "observed_payer_country": ev.get("payer_country"),
                "library_fee": q.get("processing_fee", {}).get("value"),
                "reconciliation_status": (c.reconciliation or {}).get("status"),
                "delta_minor_units": (c.reconciliation or {}).get("delta_minor_units"),
                "case_id": c.case_id,
            }
        )
    return observations


def _least_squares_fit(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Run the canonical linear formula inference over gross/fee observations."""
    if len(observations) < 2:
        return None
    return _infer_formula(observations)


def infer_formula(cases: list[Case]) -> dict[str, Any] | None:
    """Return the formula inferred from a set of manual-send cases.

    Cases must share the same preselected product and variant and have PayPal
    evidence. The returned formula combines the preselected fee-schedule
    metadata with a linear fit to the observed gross/fee pairs, allowing the
    actual sandbox fee curve to be compared with the library prediction
    regardless of whether every amount matched.
    """
    eligible = _eligible_single_scenario_cases(cases)
    if not eligible:
        return None

    observations = _collect_gross_fee_pairs(eligible)
    if len(observations) < 2:
        return None

    formula = _least_squares_fit(observations)
    if not formula or not formula.get("best"):
        return None
    best = formula["best"]

    pct = _decimal(best.get("percentage"))
    fixed = _decimal(best.get("fixed"))
    if pct is None or fixed is None:
        return None

    slope = (pct / Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    intercept = fixed.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    predictions = best.get("predictions", [])
    inferred_observations = [dict(obs, **pred) for obs, pred in zip(observations, predictions, strict=True)]

    meta = (eligible[0].quote or {}).get("_schedule_metadata") or {}
    return {
        "product_id": eligible[0].product_id,
        "variant_id": eligible[0].variant_id,
        "base_rule_id": meta.get("base_rule_id"),
        "fixed_fee_schedule_id": meta.get("fixed_fee_schedule_id"),
        "international_surcharge_schedule_id": meta.get("international_surcharge_schedule_id"),
        "payer_region": meta.get("payer_region"),
        "base_percentage": meta.get("base_percentage"),
        "fixed_amount": meta.get("fixed_amount"),
        "data_revision": (eligible[0].quote or {}).get("data", {}).get("content_sha256"),
        "inferred_from_observations": {
            "base_percentage": str(slope),
            "fixed_amount": str(intercept),
            "observations": inferred_observations,
        },
    }


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
                save_manual_case(case.run_id, case)

            results.append(case.model_dump())

            rec = case.reconciliation or {}
            if (
                rec.get("status")
                in {
                    ReconciliationStatus.FEE_MISMATCH,
                    ReconciliationStatus.NET_AMOUNT_MISMATCH,
                    ReconciliationStatus.CURRENCY_MISMATCH,
                    ReconciliationStatus.BUYER_COUNTRY_MISMATCH,
                }
                and stop_after_first_mismatch
            ):
                break

    results_dict = {"run_id": run_id, "cases": results}
    save_manual_results(run_id, results_dict)
    return results_dict
