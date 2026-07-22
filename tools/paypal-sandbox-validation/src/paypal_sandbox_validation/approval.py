from __future__ import annotations

from pathlib import Path
from typing import Any

from paypal_sandbox_validation.browser import BrowserError, PayPalBrowser
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.models import Account, ReconciliationStatus
from paypal_sandbox_validation.url_validation import URLValidationError, validate_approval_url


def _map_playwright_result_to_outcome(result: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Playwright confirmation result to a final outcome.

    Returns ``None`` when the result indicates the browser reached the approved
    state and the callback state must still be checked.  Any other status is
    returned immediately as the final outcome.
    """
    status = result.get("status")
    if status != "approved":
        return {"status": status, **{k: v for k, v in result.items() if k != "status"}}
    return None


def _map_callback_state_to_outcome(
    callback_state: str,
    result_status: str,
    screenshot_dir: Path | None,
    case_id: str | None,
    browser: PayPalBrowser | None = None,
) -> dict[str, Any]:
    """Map the return-callback state to the final approval outcome."""
    if callback_state == "cancelled":
        return {
            "status": ReconciliationStatus.BUYER_CANCELLED.value,
            "error": "Buyer cancelled the payment.",
            "issue": "BUYER_CANCELLED",
            "operation": "buyer approval",
        }
    if callback_state == "token_mismatch":
        return {
            "status": ReconciliationStatus.CALLBACK_TOKEN_MISMATCH.value,
            "error": "Callback token did not match the order token.",
            "issue": "CALLBACK_TOKEN_MISMATCH",
            "operation": "callback",
        }
    if callback_state == "timeout":
        if result_status == "approved":
            return {"status": "approved"}
        if screenshot_dir and case_id and browser:
            browser.capture_failure_screenshot(screenshot_dir / f"{case_id}-failure.png")
        return {
            "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
            "error": "Timeout waiting for return callback.",
            "issue": "BUYER_INTERACTION_BLOCKED",
            "operation": "buyer approval",
        }
    return {"status": "approved"}


def approve_order(
    buyer: Account,
    approval_url: str,
    amount: str,
    currency: str,
    order_token: str,
    headless: bool = True,
    slow_mo: int = 0,
    screenshot_dir: Path | None = None,
    case_id: str | None = None,
    callback_server: CallbackServer | None = None,
) -> dict[str, str]:
    """Run the Playwright buyer approval flow.

    If ``callback_server`` is provided it is assumed to already be started and
    will not be stopped by this function.
    """
    require_sandbox_approval_url(approval_url)
    manage_callback = callback_server is None
    if callback_server is None:
        callback_server = CallbackServer(expected_token=order_token)
        callback_server.start()
    else:
        callback_server.update_expected_token(order_token)

    try:
        with PayPalBrowser(headless=headless, slow_mo=slow_mo) as browser:
            browser.open_approval_url(approval_url)
            browser.login(buyer.primary_email_alias, buyer.password)
            screenshot_path = None
            if screenshot_dir and case_id:
                screenshot_path = screenshot_dir / f"{case_id}-checkout.png"
            result = browser.confirm_and_approve(amount, currency, screenshot_path=screenshot_path)

            playwright_outcome = _map_playwright_result_to_outcome(result)
            if playwright_outcome is not None:
                return playwright_outcome

            callback_state = callback_server.wait_for_state(timeout=120.0)
            return _map_callback_state_to_outcome(
                callback_state,
                result.get("status", ""),
                screenshot_dir,
                case_id,
                browser,
            )
    except BrowserError as exc:
        return {
            "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
            "error": str(exc),
            "issue": "BUYER_INTERACTION_BLOCKED",
            "operation": "buyer approval",
        }
    finally:
        if manage_callback:
            callback_server.stop()


def require_sandbox_approval_url(url: str) -> None:
    try:
        validate_approval_url(url)
    except URLValidationError as exc:
        raise BrowserError(str(exc)) from exc
