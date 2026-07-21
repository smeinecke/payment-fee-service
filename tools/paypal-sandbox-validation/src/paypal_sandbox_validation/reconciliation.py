from __future__ import annotations

from typing import Any

from paypal_sandbox_validation.models import ReconciliationResult, ReconciliationStatus
from paypal_sandbox_validation.numeric import _decimal
from paypal_sandbox_validation.quote_adapter import minor_units


def reconcile(
    paypal_evidence: dict[str, Any],
    quote: dict[str, Any],
    merchant_country: str,
    buyer_country: str,
    observed_payer_country: str | None = None,
) -> ReconciliationResult:
    gross = paypal_evidence.get("gross_amount", {}) or paypal_evidence.get("amount", {}) or {}
    fee = paypal_evidence.get("paypal_fee", {}) or {}
    if not fee:
        breakdown = (paypal_evidence.get("seller_receivable") or {}).get("breakdown", {})
        fee = breakdown.get("paypal_fee", {}) if breakdown else {}
    net = paypal_evidence.get("net_amount", {}) or {}

    gross_currency = gross.get("currency_code", "")
    gross_value = _decimal(gross.get("value"))
    fee_currency = fee.get("currency_code", "")
    fee_value = _decimal(fee.get("value"))
    net_currency = net.get("currency_code", "")
    net_value = _decimal(net.get("value"))

    library = quote or {}
    lib_fee_value = _decimal(library.get("processing_fee", {}).get("value"))
    lib_fee_currency = library.get("processing_fee", {}).get("currency", "")
    lib_net_value = _decimal(library.get("net_amount", {}).get("value"))

    result = ReconciliationResult(
        status=ReconciliationStatus.MATCH,
        merchant_country=merchant_country,
        buyer_country=buyer_country,
        observed_payer_country=observed_payer_country,
        gross_currency=gross_currency,
        gross_value=str(gross_value) if gross_value is not None else None,
        paypal_fee_value=str(fee_value) if fee_value is not None else None,
        paypal_fee_currency=fee_currency,
        library_fee_value=str(lib_fee_value) if lib_fee_value is not None else None,
        library_fee_currency=lib_fee_currency,
        paypal_net_value=str(net_value) if net_value is not None else None,
        library_net_value=str(lib_net_value) if lib_net_value is not None else None,
    )

    # Buyer country verification takes precedence over fee comparison.
    if observed_payer_country and observed_payer_country.upper() != buyer_country.upper():
        result.status = ReconciliationStatus.BUYER_COUNTRY_MISMATCH
        result.root_cause = "configured buyer country does not match observed payer country"
        return result

    if gross_value is None:
        result.status = ReconciliationStatus.PAYPAL_API_FAILURE
        result.root_cause = "harness defect"
        return result

    if fee_value is None:
        result.status = ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE
        result.root_cause = "unsupported Sandbox behavior"
        return result

    if lib_fee_value is None:
        result.status = ReconciliationStatus.LIBRARY_NOT_CALCULABLE
        result.root_cause = "library_missing_context"
        return result

    if fee_currency != lib_fee_currency:
        result.status = ReconciliationStatus.CURRENCY_MISMATCH
        result.root_cause = "fee-data defect"
        return result

    if gross_currency != gross.get("currency_code") or net_currency != gross_currency:
        result.status = ReconciliationStatus.CURRENCY_MISMATCH
        result.root_cause = "fee-data defect"
        return result

    # Verify PayPal gross - fee == net using exact Decimal arithmetic.
    if gross_value is not None and fee_value is not None and net_value is not None:
        expected_net = gross_value - fee_value
        if expected_net != net_value:
            result.status = ReconciliationStatus.NET_AMOUNT_MISMATCH
            result.root_cause = "rounding defect"
            return result

    # Compare fees in minor units.
    paypal_fee_minor = minor_units(fee_value, fee_currency) if fee_value is not None else None
    library_fee_minor = minor_units(lib_fee_value, lib_fee_currency) if lib_fee_value is not None else None
    result.paypal_fee_minor = paypal_fee_minor
    result.library_fee_minor = library_fee_minor

    if paypal_fee_minor is not None and library_fee_minor is not None and paypal_fee_minor != library_fee_minor:
        result.status = ReconciliationStatus.FEE_MISMATCH
        result.delta_minor_units = paypal_fee_minor - library_fee_minor
        result.root_cause = _classify_root_cause(result)
        return result

    result.delta_minor_units = 0
    result.components = quote.get("components", [])
    result.matched_rules = [
        str(rule_id)
        for rule_id in [
            r.get("rule_id") if isinstance(r, dict) else getattr(r, "rule_id", None)
            for r in quote.get("matched_rules", [])
        ]
        if rule_id is not None
    ]
    result.amount = str(gross_value) if gross_value is not None else None
    result.currency = gross_currency or None
    meta = quote.get("_schedule_metadata") or {}
    result.base_rule_id = meta.get("base_rule_id")
    result.fixed_fee_schedule_id = meta.get("fixed_fee_schedule_id")
    result.international_surcharge_schedule_id = meta.get("international_surcharge_schedule_id")
    result.base_percentage = meta.get("base_percentage")
    result.fixed_amount = meta.get("fixed_amount")
    result.surcharge_percentage = meta.get("surcharge_percentage")
    result.predicted_total_fee = meta.get("predicted_total_fee")
    result.payer_region = meta.get("payer_region")
    return result


def _classify_root_cause(result: ReconciliationResult) -> str:
    if result.observed_payer_country and result.observed_payer_country != result.buyer_country:
        return "payer-region mapping defect"
    if result.delta_minor_units and abs(result.delta_minor_units) == 1:
        return "rounding defect"
    return "fee-data defect"
