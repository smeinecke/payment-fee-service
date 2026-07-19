from __future__ import annotations

from decimal import Decimal
from typing import Any

from paypal_sandbox_validation.models import ReconciliationStatus
from paypal_sandbox_validation.paypal_api import PayPalAPIError, PayPalClient


def capture_order(
    client: PayPalClient,
    order_id: str,
    request_id_capture: str,
) -> dict[str, Any]:
    """Capture an approved order and return the capture evidence."""
    order = client.get_order(order_id)
    if order.get("status") != "APPROVED":
        raise PayPalAPIError(f"Order not approved before capture: {order.get('status')}")

    capture_response = client.capture_order(order_id, request_id_capture)
    if capture_response.get("status") not in {"COMPLETED", "PENDING"}:
        raise PayPalAPIError(f"Capture did not complete: {capture_response.get('status')}")

    return extract_capture_evidence(capture_response, order)


def extract_capture_evidence(order_or_capture: dict[str, Any], order: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract the required fee evidence from a capture response or payment capture."""
    capture = order_or_capture
    if "purchase_units" in order_or_capture and order_or_capture.get("purchase_units"):
        payments = order_or_capture["purchase_units"][0].get("payments", {})
        captures = payments.get("captures", [])
        if captures:
            capture = captures[0]

    if not isinstance(capture, dict):
        raise PayPalAPIError("Capture response is not a valid object")

    breakdown = capture.get("seller_receivable_breakdown", {}) or {}
    gross = _money(breakdown.get("gross_amount"))
    fee = _money(breakdown.get("paypal_fee"))
    net = _money(breakdown.get("net_amount"))
    receivable = _money(breakdown.get("receivable_amount"))
    exchange_rate = breakdown.get("exchange_rate")

    payer_country = None
    if order and isinstance(order.get("payer"), dict):
        payer = order["payer"]
        address = payer.get("address", {})
        payer_country = address.get("country_code") or payer.get("payer_info", {}).get("country_code")

    evidence = {
        "capture_id": capture.get("id"),
        "status": capture.get("status"),
        "gross_amount": gross,
        "paypal_fee": fee,
        "net_amount": net,
        "receivable_amount": receivable,
        "exchange_rate": exchange_rate,
        "payer_country": payer_country,
    }

    if gross is None or gross.get("value") is None:
        raise PayPalAPIError("Missing gross_amount in capture response")
    if fee is None or fee.get("value") is None:
        evidence["paypal_fee_missing"] = True
        evidence["status_detail"] = ReconciliationStatus.PAYPAL_FEE_UNAVAILABLE.value

    return evidence


def _money(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {
        "currency_code": value.get("currency_code", ""),
        "value": value.get("value", ""),
    }


def money_to_decimal(money: dict[str, Any] | None) -> Decimal | None:
    if not money or not money.get("value"):
        return None
    try:
        return Decimal(str(money["value"]))
    except Exception:
        return None
