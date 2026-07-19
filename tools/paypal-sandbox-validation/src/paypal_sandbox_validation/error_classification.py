from __future__ import annotations

from typing import Any

from paypal_sandbox_validation.models import ReconciliationStatus
from paypal_sandbox_validation.paypal_api import PayPalAPIError, extract_paypal_error_fields, extract_paypal_issue


def classify_paypal_api_error(exc: PayPalAPIError) -> tuple[ReconciliationStatus, dict[str, Any], str]:
    """Map a PayPal API failure to a ReconciliationStatus and sanitized details."""
    safe = extract_paypal_error_fields(exc)
    issue = extract_paypal_issue(exc.body)
    status = ReconciliationStatus.PAYPAL_API_FAILURE
    detail = safe.get("description") or "PayPal API failure"

    if issue and "COMPLIANCE" in issue:
        status = ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE
        detail = f"PayPal compliance violation during {safe.get('operation')}"
    elif issue == "INVALID_CLIENT" or safe.get("error") == "invalid_client":
        status = ReconciliationStatus.AUTHENTICATION_FAILED
        detail = f"PayPal client authentication failed during {safe.get('operation')}"
    elif issue in {"CURRENCY_NOT_SUPPORTED", "PAYMENT_METHOD_NOT_SUPPORTED", "PAYEE_ACCOUNT_RESTRICTED"}:
        status = ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE
        detail = f"PayPal account/payment restriction during {safe.get('operation')}"
    elif issue == "ORDER_NOT_APPROVED":
        status = ReconciliationStatus.PAYPAL_API_FAILURE
        detail = f"Order not approved before capture: {safe.get('description')}"
    elif safe.get("http_status") in {429, 500, 502, 503, 504}:
        status = ReconciliationStatus.PAYPAL_API_FAILURE
        detail = f"Transient PayPal API failure during {safe.get('operation')}"

    safe["detail"] = detail
    return status, safe, detail
