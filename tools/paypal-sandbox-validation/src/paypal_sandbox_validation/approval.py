from __future__ import annotations

from pathlib import Path

from paypal_sandbox_validation.browser import BrowserError, PayPalBrowser
from paypal_sandbox_validation.callback_server import CallbackServer
from paypal_sandbox_validation.models import Account, ReconciliationStatus
from paypal_sandbox_validation.url_validation import URLValidationError, validate_approval_url


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
            if result == ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value:
                return {"status": result, "error": "PayPal presented a security challenge."}
            if result == "compliance_violation":
                return {
                    "status": ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE.value,
                    "error": "PayPal Sandbox compliance violation; the account configuration may be incomplete.",
                }
            callback_state = callback_server.wait_for_state(timeout=120.0)
            if callback_state == "cancelled":
                return {
                    "status": ReconciliationStatus.BUYER_CANCELLED.value,
                    "error": "Buyer cancelled the payment.",
                }
            if callback_state == "token_mismatch":
                return {
                    "status": "callback_token_mismatch",
                    "error": "Callback token did not match the order token.",
                }
            if callback_state == "timeout":
                if result == "approved":
                    return {"status": "approved"}
                if screenshot_dir and case_id:
                    browser.capture_failure_screenshot(screenshot_dir / f"{case_id}-failure.png")
                return {
                    "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
                    "error": "Timeout waiting for return callback.",
                }
            return {"status": "approved"}
    except BrowserError as exc:
        return {"status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value, "error": str(exc)}
    finally:
        if manage_callback:
            callback_server.stop()


def require_sandbox_approval_url(url: str) -> None:
    try:
        validate_approval_url(url)
    except URLValidationError as exc:
        raise BrowserError(str(exc)) from exc
