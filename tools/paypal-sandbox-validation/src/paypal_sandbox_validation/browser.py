from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from paypal_sandbox_validation.models import ReconciliationStatus
from paypal_sandbox_validation.url_validation import URLValidationError, validate_approval_url


class BrowserError(Exception):
    pass


class PayPalBrowser:
    def __init__(self, headless: bool = True, slow_mo: int = 0) -> None:
        self.headless = headless
        self.slow_mo = slow_mo
        self.playwright: Playwright | None = None
        self.browser = None
        self.context = None
        self.page: Page | None = None

    def __enter__(self) -> PayPalBrowser:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self.context = self.browser.new_context(
            accept_downloads=False,
            bypass_csp=True,
        )
        self.context.clear_cookies()
        self.page = self.context.new_page()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def _require_page(self) -> Page:
        if not self.page:
            raise BrowserError("Browser page not initialized.")
        return self.page

    def open_approval_url(self, url: str) -> None:
        try:
            validate_approval_url(url)
        except URLValidationError as exc:
            raise BrowserError(str(exc)) from exc
        page = self._require_page()
        page.goto(url, timeout=120000)

    def login(self, email: str, password: str) -> None:
        page = self._require_page()
        try:
            self._fill_email(page, email)
            self._fill_password(page, password)
        except PlaywrightTimeoutError as exc:
            raise BrowserError("Buyer login form not available", exc) from exc

    def _fill_email(self, page: Page, email: str) -> None:
        email_input = page.locator("input[name='login_email'], input#email, input[type='email']").first
        if not email_input.is_visible(timeout=5000):
            return
        email_input.fill(email)
        next_btn = page.locator("button#btnNext").first
        if next_btn.is_visible(timeout=3000):
            next_btn.click()
            # Wait for the spinner to disappear and the password section to appear.
            page.locator("div.transitioning.spinner").wait_for(state="hidden", timeout=15000)
        page.wait_for_selector("input[name='login_password'], input#password, input[type='password']", timeout=15000)

    def _fill_password(self, page: Page, password: str) -> None:
        pw_input = page.locator("input[name='login_password'], input#password, input[type='password']").first
        pw_input.wait_for(state="visible", timeout=15000)
        pw_input.fill(password)
        login_btn = page.locator("button#btnLogin").first
        login_btn.wait_for(state="visible", timeout=15000)
        login_btn.click()

    def confirm_and_approve(
        self,
        amount: str,
        currency: str,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        page = self._require_page()
        try:
            self._verify_amount(page, amount, currency)
            self._select_balance_if_available(page)
            self._click_pay_now(page)
            page.wait_for_url("**/paypal/return**", timeout=60000)
            return {"status": "approved"}
        except PlaywrightTimeoutError:
            if screenshot_path:
                self._safe_screenshot(screenshot_path)
            evidence = self.capture_checkout_evidence(
                api_issue=self._extract_url_issue(page.url),
                operation="buyer approval",
            )
            if self._is_challenge_present(page):
                return {
                    "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
                    "issue": "BUYER_INTERACTION_BLOCKED",
                    "operation": "buyer approval",
                    "evidence": evidence,
                    "error": "PayPal presented a security challenge.",
                }
            generic_error = self._detect_generic_error(page)
            if generic_error:
                return {**generic_error, "evidence": evidence}
            return {
                "status": ReconciliationStatus.BUYER_CHECKOUT_UNKNOWN_ERROR.value,
                "issue": "BUYER_CHECKOUT_UNKNOWN_ERROR",
                "operation": "buyer approval",
                "evidence": evidence,
                "error": "Timed out waiting for buyer approval",
            }

    def _verify_amount(self, page: Page, amount: str, currency: str) -> None:
        # Allow localized decimal separators and minor formatting differences.
        amount_re = re.escape(amount).replace(r"\.", r"[.,]")
        symbol = re.escape(self._currency_symbol(currency).upper())
        pattern = re.compile(
            rf"{amount_re}\s*{symbol}|{symbol}\s*{amount_re}|"
            rf"{amount_re}\s*{re.escape(currency)}|{re.escape(currency)}\s*{amount_re}",
            re.IGNORECASE,
        )
        try:
            page.get_by_text(pattern).first.wait_for(timeout=10000)
        except PlaywrightTimeoutError as exc:
            raise BrowserError(f"Amount/currency not confirmed on checkout page: {amount} {currency}") from exc

    def _currency_symbol(self, currency: str) -> str:
        symbols = {
            "EUR": "€",
            "GBP": "£",
            "USD": "$",
            "JPY": "¥",
            "CAD": "C$",
            "AUD": "A$",
            "CHF": "CHF",
            "BRL": "R$",
            "HKD": "HK$",
            "CZK": "Kč",
            "ILS": "₪",
            "ZAR": "R",
        }
        return symbols.get(currency, currency).lower()

    def _select_balance_if_available(self, page: Page) -> None:
        balance_patterns = re.compile(r"PayPal balance|PayPal-Guthaben|Guthaben|Balance|balance", re.IGNORECASE)
        try:
            locator = page.get_by_text(balance_patterns).first
            if locator.is_visible(timeout=3000):
                locator.click()
        except PlaywrightTimeoutError:
            pass

    def _click_pay_now(self, page: Page) -> None:
        pay_pattern = re.compile(
            r"Pay Now|Complete Purchase|Jetzt zahlen|Zahlung bestätigen|"
            r"Jetzt bezahlen|Bestätigen|Approve|Continue|Weiter|Zahlen|Pay|Agree",
            re.IGNORECASE,
        )
        try:
            button = page.get_by_role("button", name=pay_pattern).first
            button.wait_for(state="visible", timeout=5000)
            button.click()
            return
        except PlaywrightTimeoutError:
            pass
        raise BrowserError("No Pay Now/Approve button found on checkout page")

    _KNOWN_COMPLIANCE_MESSAGES: ClassVar[frozenset[str]] = frozenset(
        {
            "this transaction cannot be completed due to a compliance violation",
            "your account is restricted",
            "we're unable to complete this transaction",
            "payment cannot be processed due to a compliance issue",
        }
    )

    def _is_challenge_present(self, page: Page) -> bool:
        challenge_terms = ["captcha", "security challenge", "verify", "2-step", "mfa", "sicherheitsprüfung"]
        text = page.content().lower()
        return any(term in text for term in challenge_terms)

    def _extract_url_issue(self, url: str) -> str | None:
        """Extract a structured PayPal API issue from a genericError URL code."""
        if "genericError" not in url:
            return None
        parsed = urlparse(url)
        code_list = parse_qs(parsed.query).get("code")
        if not code_list:
            return None
        code = code_list[0]
        try:
            decoded = base64.b64decode(code).decode("ascii", errors="replace")
        except Exception:
            return None
        decoded_upper = decoded.upper()
        if "COMPLIANCE" in decoded_upper:
            return "COMPLIANCE_VIOLATION"
        if "DENIED" in decoded_upper:
            return "BUYER_INTERACTION_BLOCKED"
        if "CANCEL" in decoded_upper:
            return "BUYER_CANCELLED"
        return None

    def _is_compliance_message(self, text: str) -> bool:
        """Require an exact known compliance phrase before classifying compliance."""
        text_lower = text.lower()
        return any(phrase in text_lower for phrase in self._KNOWN_COMPLIANCE_MESSAGES)

    def _detect_generic_error(self, page: Page) -> dict[str, Any] | None:
        url = page.url
        if "genericError" not in url:
            return None
        api_issue = self._extract_url_issue(url)
        evidence = self.capture_checkout_evidence(api_issue=api_issue, operation="buyer approval")

        if api_issue == "COMPLIANCE_VIOLATION":
            return {
                "status": ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE.value,
                "issue": "COMPLIANCE_VIOLATION",
                "operation": "buyer approval",
                "error": "PayPal Sandbox compliance violation detected.",
                "evidence": evidence,
            }
        if api_issue == "BUYER_INTERACTION_BLOCKED":
            return {
                "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
                "issue": "BUYER_INTERACTION_BLOCKED",
                "operation": "buyer approval",
                "error": "PayPal denied the transaction.",
                "evidence": evidence,
            }

        # Visible error text is a secondary signal; never classify compliance from a broad substring.
        visible_text = evidence.get("error_message", "")
        if self._is_compliance_message(visible_text):
            return {
                "status": ReconciliationStatus.ACCOUNT_CONFIGURATION_DIFFERENCE.value,
                "issue": "COMPLIANCE_VIOLATION",
                "operation": "buyer approval",
                "error": "PayPal Sandbox compliance violation detected from visible error text.",
                "evidence": evidence,
            }
        return {
            "status": ReconciliationStatus.BUYER_CHECKOUT_UNKNOWN_ERROR.value,
            "issue": "BUYER_CHECKOUT_UNKNOWN_ERROR",
            "operation": "buyer approval",
            "error": "PayPal returned a generic checkout error.",
            "evidence": evidence,
        }

    def capture_checkout_evidence(
        self,
        api_issue: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Collect sanitized checkout-page evidence for diagnostic reports."""
        page = self._require_page()
        from urllib.parse import urlparse

        url = page.url
        parsed = urlparse(url)
        title = self._safe_text(page.title())
        heading = self._safe_text(self._extract_visible_error_heading(page))
        message = self._safe_text(self._extract_visible_error_message(page))

        return {
            "hostname": parsed.hostname,
            "pathname": parsed.path,
            "page_title": title,
            "error_heading": heading,
            "error_message": message,
            "checkout_step": self._checkout_step(parsed.path, page.content().lower()),
            "paypal_api_issue": api_issue,
            "operation": operation,
        }

    def _extract_visible_error_heading(self, page: Page) -> str:
        selectors = ["h1", "h2", "h3", "[data-testid='error-heading']", ".error-heading"]
        for selector in selectors:
            try:
                return page.locator(selector).first.inner_text(timeout=3000).strip()
            except PlaywrightTimeoutError:
                continue
        return ""

    def _extract_visible_error_message(self, page: Page) -> str:
        selectors = ["[role='alert']", ".alert", ".error-message", ".error", ".message", "p"]
        for selector in selectors:
            try:
                text = page.locator(selector).first.inner_text(timeout=3000).strip()
                if text:
                    return text
            except PlaywrightTimeoutError:
                continue
        return ""

    def _checkout_step(self, pathname: str, content_lower: str) -> str:
        if "login" in pathname or "signin" in pathname or "input[name='login" in content_lower:
            return "login"
        if "review" in pathname or "checkout" in pathname or "payment" in content_lower:
            return "review_payment"
        if "genericError" in pathname:
            return "generic_error"
        if "return" in pathname:
            return "return"
        return "unknown"

    def _safe_text(self, text: str | None) -> str:
        if not text:
            return ""
        # Redact any email addresses or long tokens that appear in page text.
        from paypal_sandbox_validation.redaction import redact_text

        return redact_text(text)

    def _safe_screenshot(self, path: Path) -> None:
        page = self._require_page()
        if page.is_visible("input[type='password']", timeout=500) or page.is_visible(
            "input[name='login_email']", timeout=500
        ):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))

    def capture_failure_screenshot(self, path: Path) -> bool:
        page = self._require_page()
        try:
            if page.is_visible("input[type='password']", timeout=500):
                return False
            if page.is_visible("input[name='login_email']", timeout=500):
                return False
        except PlaywrightTimeoutError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        return path.exists()
