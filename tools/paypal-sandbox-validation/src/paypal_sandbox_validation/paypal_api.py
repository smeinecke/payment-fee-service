from __future__ import annotations

import copy
from typing import Any

import httpx

from paypal_sandbox_validation.redaction import sanitize_paypal_capture, sanitize_paypal_order

PAYPAL_SANDBOX_API = "https://api-m.sandbox.paypal.com"
PAYPAL_SANDBOX_CHECKOUT = "https://www.sandbox.paypal.com"

ALLOWED_HOSTS = {
    "api-m.sandbox.paypal.com",
    "www.sandbox.paypal.com",
}

LIVE_HOSTS = {
    "api-m.paypal.com",
    "www.paypal.com",
    "api.paypal.com",
}


def require_sandbox_host(url: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host in LIVE_HOSTS or "live" in host or "production" in host:
        raise ValueError(f"Live PayPal host rejected: {url}")
    if host not in ALLOWED_HOSTS and "sandbox" not in host:
        raise ValueError(f"Only PayPal Sandbox hosts are allowed: {url}")


class PayPalAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


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
        require_sandbox_host(url)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=self._headers(request_id), json=payload)
        return self._handle(response, "create order")

    def get_order(self, order_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/checkout/orders/{order_id}"
        require_sandbox_host(url)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=self._headers())
        return self._handle(response, "get order")

    def capture_order(self, order_id: str, request_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        require_sandbox_host(url)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=self._headers(request_id))
        return self._handle(response, "capture order")

    def get_capture(self, capture_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/v2/payments/captures/{capture_id}"
        require_sandbox_host(url)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=self._headers())
        return self._handle(response, "get capture")

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
        )


def extract_approval_url(order: dict[str, Any]) -> str:
    for rel in ("payer-action", "approve"):
        for link in order.get("links", []):
            if link.get("rel") == rel and link.get("href"):
                return link["href"]
    raise PayPalAPIError("No approval link found in order response")


def build_order_payload(
    amount: str,
    currency: str,
    return_url: str,
    cancel_url: str,
    reference_id: str,
    invoice_id: str,
    custom_id: str,
) -> dict[str, Any]:
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
        },
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
