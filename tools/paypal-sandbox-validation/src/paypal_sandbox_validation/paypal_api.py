from __future__ import annotations

import copy
from typing import Any

import httpx

from paypal_sandbox_validation.redaction import sanitize_paypal_capture, sanitize_paypal_order
from paypal_sandbox_validation.url_validation import validate_api_url

PAYPAL_SANDBOX_API = "https://api-m.sandbox.paypal.com"
PAYPAL_SANDBOX_CHECKOUT = "https://www.sandbox.paypal.com"


def require_sandbox_host(url: str) -> None:
    """Backward-compatible alias for the exact API URL validator."""
    validate_api_url(url)


class PayPalAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: dict[str, Any] | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}
        self.operation = operation


class PayPalClient:
    def __init__(self, token: str, timeout: float = 60.0) -> None:
        self.token = token
        self.timeout = timeout
        self.base_url = PAYPAL_SANDBOX_API
        require_sandbox_host(self.base_url)

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return headers

    def create_order(
        self,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/v2/checkout/orders"
        return self._request("POST", url, "create order", json=payload, request_id=request_id)

    def get_order(self, order_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/checkout/orders/{order_id}"
        return self._request("GET", url, "get order")

    def capture_order(self, order_id: str, request_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        return self._request("POST", url, "capture order", request_id=request_id)

    def get_capture(self, capture_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/payments/captures/{capture_id}"
        return self._request("GET", url, "get capture")

    def _request(
        self,
        method: str,
        url: str,
        operation: str,
        json: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from time import sleep

        require_sandbox_host(url)
        attempts = 3
        response: httpx.Response | None = None
        for attempt in range(attempts):
            with httpx.Client(timeout=self.timeout) as client:
                if method == "GET":
                    response = client.get(url, headers=self._headers(request_id))
                elif method == "POST":
                    response = client.post(url, headers=self._headers(request_id), json=json)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
            if response.status_code < 300 or response.status_code not in {429, 500, 502, 503, 504}:
                return self._handle(response, operation)
            if attempt < attempts - 1:
                sleep(2**attempt)
        assert response is not None
        return self._handle(response, operation)

    def _handle(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            body = {}
        if response.status_code < 300:
            return body
        raise PayPalAPIError(
            f"PayPal API failure during {operation}: HTTP {response.status_code}",
            status_code=response.status_code,
            body=body,
            operation=operation,
        )


def extract_approval_url(order: dict[str, Any]) -> str:
    from paypal_sandbox_validation.url_validation import validate_approval_url

    for rel in ("payer-action", "approve"):
        for link in order.get("links", []):
            href = link.get("href")
            if link.get("rel") == rel and href:
                validate_approval_url(href)
                return href
    raise PayPalAPIError("No approval link found in order response", operation="extract approval url")


def build_order_payload(
    amount: str,
    currency: str,
    return_url: str,
    cancel_url: str,
    reference_id: str,
    invoice_id: str,
    custom_id: str,
    brand_name: str = "PayPal Sandbox Validation",
) -> dict[str, Any]:
    from paypal_sandbox_validation.url_validation import validate_callback_url

    validate_callback_url(return_url)
    validate_callback_url(cancel_url)

    # PayPal's current Sandbox checkout flow returns a classic `approve`
    # link when application_context is supplied.  The newer
    # payment_source.paypal.experience_context shape triggers the
    # checkoutweb generic error page for several sandbox merchants, so we
    # fall back to the still-supported application_context contract.
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": reference_id,
                "invoice_id": invoice_id,
                "custom_id": custom_id,
                "amount": {
                    "currency_code": currency,
                    "value": amount,
                },
            }
        ],
        "application_context": {
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": return_url,
            "cancel_url": cancel_url,
            "brand_name": brand_name,
        },
    }


def extract_paypal_issue(body: dict[str, Any]) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("error", "name"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value.upper()
    details = body.get("details") or []
    if isinstance(details, list) and details:
        issue = details[0].get("issue") or ""
        if isinstance(issue, str):
            return issue.upper()
    return None


def extract_paypal_error_fields(exc: PayPalAPIError) -> dict[str, Any]:
    """Return only the sanitized, non-credential PayPal error fields."""
    body = exc.body or {}
    details = body.get("details") or []
    first_detail = details[0] if isinstance(details, list) and details else {}
    return {
        "http_status": exc.status_code,
        "operation": exc.operation,
        "error": body.get("error"),
        "name": body.get("name"),
        "issue": first_detail.get("issue") if isinstance(first_detail, dict) else None,
        "description": first_detail.get("description") if isinstance(first_detail, dict) else body.get("message"),
        "debug_id": body.get("debug_id"),
        "information_link": body.get("information_link"),
    }


def sanitize_capture_evidence(capture: dict[str, Any]) -> dict[str, Any]:
    capture = copy.deepcopy(capture)
    safe = sanitize_paypal_capture(capture)
    if "seller_receivable_breakdown" in safe:
        breakdown = safe["seller_receivable_breakdown"]
        for k in ["gross_amount", "paypal_fee", "net_amount", "receivable_amount"]:
            if k in breakdown:
                breakdown[k] = breakdown[k]
    return safe


def sanitize_order_response(order: dict[str, Any]) -> dict[str, Any]:
    return sanitize_paypal_order(order)
