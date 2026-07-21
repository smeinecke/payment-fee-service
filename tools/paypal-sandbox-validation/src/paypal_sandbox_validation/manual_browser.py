"""Playwright automation for the PayPal Sandbox manual Send Money buyer UI.

Only ``www.sandbox.paypal.com`` is allowed. No Orders-v2, REST or NVP/SOAP.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from paypal_sandbox_validation.accounts import Account
from paypal_sandbox_validation.browser_common import _fill_paypal_login_form
from paypal_sandbox_validation.models import ReconciliationStatus
from paypal_sandbox_validation.redaction import redact_text


class ManualBrowserError(Exception):
    pass


class UnsupportedPayPalUIState(ManualBrowserError):
    pass


class ManualPaymentBrowser:
    """Playwright wrapper for the PayPal Sandbox Send Money UI flow."""

    # Host allowlist for navigation.
    _ALLOWED_HOSTS: ClassVar[frozenset[str]] = frozenset({"www.sandbox.paypal.com"})

    # Localized labels used to detect UI state without CSS classes.
    _LABELS: ClassVar[dict[str, dict[str, str]]] = {
        "de": {
            "send_tab": "Senden",
            "request_tab": "Anfordern",
            "continue": "Weiter",
            "amount_input_aria": "Betragsfeld eingeben",
            "note_placeholder": "Eine Mitteilung hinzufügen",
            "balance": "PayPal-Guthaben",
            "goods_and_services": "Waren und Dienstleistungen",
            "goods_and_services_alt": "Geld für Waren und Dienstleistungen senden/empfangen",
            "friends_and_family": "Familie und Freunde",
            "fee_label": "PayPal-Gebühr",
            "total_label": "Summe",
            "success_title": "Geld senden",
            "payment_from": "Payment from",
            "completed": "Completed",
            "recipient_gets": "empfängt",
        },
        "en": {
            "send_tab": "Send",
            "request_tab": "Request",
            "continue": "Continue",
            "amount_input_aria": "Enter amount",
            "note_placeholder": "Add a note",
            "balance": "PayPal balance",
            "goods_and_services": "Goods and Services",
            "goods_and_services_alt": "Sending or receiving money for goods and services",
            "friends_and_family": "Friends and Family",
            "fee_label": "PayPal fee",
            "total_label": "Total",
            "success_title": "Send Money",
            "payment_from": "Payment from",
            "completed": "Completed",
            "recipient_gets": "receives",
        },
    }

    def __init__(self, headless: bool = True, slow_mo: int = 0) -> None:
        self.headless = headless
        self.slow_mo = slow_mo
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.locale = "en"

    def __enter__(self) -> ManualPaymentBrowser:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self.context:
            self.context.close()
            self.context = None
        if self.browser:
            self.browser.close()
            self.browser = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None

    def _new_context(self) -> BrowserContext:
        if not self.browser:
            raise ManualBrowserError("Browser not launched.")
        if self.context:
            self.context.close()
        context = self.browser.new_context(
            accept_downloads=False,
            bypass_csp=True,
        )
        context.clear_cookies()
        self.context = context
        self.page = context.new_page()
        return context

    def _require_page(self) -> Page:
        if not self.page:
            raise ManualBrowserError("No page available.")
        return self.page

    def _validate_url(self, url: str) -> None:
        host = urlparse(url).hostname
        if host not in self._ALLOWED_HOSTS:
            raise ManualBrowserError(f"Navigation to non-Sandbox PayPal host rejected: {url}")

    def _is_challenge_present(self, page: Page) -> bool:
        url = page.url.lower()
        path = urlparse(page.url).path.lower()
        challenge_url_fragments = ["/challenge/", "/login/challenge", "/verify/"]
        if any(fragment in path or fragment in url for fragment in challenge_url_fragments):
            return True
        challenge_selectors = [
            "input#security_code",
            "input[name='security_code']",
            "input[name='otp']",
            "input[name='captcha']",
            "[data-nemo='captcha']",
        ]
        return any(page.locator(selector).count() > 0 for selector in challenge_selectors)

    def _detect_locale(self, page: Page) -> str:
        title = page.title()
        if any(k in title for k in ["Geld senden", "Vorschau", "Auswahl"]):
            return "de"
        return "en"

    def _label(self, key: str) -> str:
        return self._LABELS[self.locale].get(key, self._LABELS["en"][key])

    def _accept_cookies(self, page: Page) -> None:
        for name in ["Akzeptieren", "Accept", "Zustimmen"]:
            try:
                btn = page.locator("button").filter(has_text=name).first
                if btn.is_visible(timeout=3000):
                    btn.click(force=True)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                pass

    def login(self, account: Account) -> None:
        """Log in to a PayPal Sandbox account in a fresh browser context."""
        self._new_context()
        page = self._require_page()
        if not account.primary_email_alias or not account.password:
            raise ManualBrowserError(f"Missing credentials for {account.country_code} {account.account_type.value}")

        url = (
            "https://www.sandbox.paypal.com/signin"
            "?returnUri=https%3A%2F%2Fwww.sandbox.paypal.com%2Fmyaccount%2Ftransfer"
        )
        self._validate_url(url)
        page.goto(url, timeout=120000)

        _fill_paypal_login_form(page, account.primary_email_alias, account.password)
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        if self._is_challenge_present(page):
            raise ManualBrowserError("PayPal presented a security challenge at login")

        self.locale = self._detect_locale(page)
        self._accept_cookies(page)

    def logout(self) -> None:
        """Log out and destroy the current context."""
        if self.context:
            self.context.close()
            self.context = None
            self.page = None

    def _navigate(self, path: str) -> None:
        url = f"https://www.sandbox.paypal.com{path}"
        self._validate_url(url)
        self._require_page().goto(url, timeout=120000)
        self._require_page().wait_for_timeout(3000)

    def _select_recipient(self, merchant: Account) -> None:
        page = self._require_page()
        if not merchant.primary_email_alias:
            raise ManualBrowserError("Business account has no email alias")
        page.goto("https://www.sandbox.paypal.com/myaccount/transfer/homepage/pay", timeout=120000)
        page.wait_for_timeout(3000)

        recipient_input = page.locator("[data-nemo='recipient']").first
        recipient_input.fill(merchant.primary_email_alias)
        page.wait_for_timeout(1500)

        # Select the suggested contact if it appears; otherwise press Enter.
        recipient_name = f"{merchant.first_name} {merchant.last_name}".strip()
        suggestion = (
            page.locator("[peertype='USER']").filter(has_text=recipient_name or merchant.primary_email_alias).first
        )
        if suggestion.is_visible(timeout=5000):
            suggestion.click(force=True)
            page.wait_for_timeout(1000)
        else:
            recipient_input.press("Enter")
            page.wait_for_timeout(1000)

        # The UI may advance directly to the amount page after selecting a recipient.
        if "/buy/preview" in urlparse(page.url).path:
            return

        try:
            continue_btn = self._find_continue_button()
            continue_btn.click(force=True)
            page.wait_for_timeout(4000)
        except UnsupportedPayPalUIState:
            if "/buy/preview" not in urlparse(page.url).path:
                raise

    def _find_continue_button(self) -> Any:
        page = self._require_page()
        patterns = ["Weiter", "Continue", "Next"]
        for pattern in patterns:
            try:
                btn = page.locator("button").filter(has_text=re.compile(pattern, re.IGNORECASE)).first
                if btn.is_visible(timeout=3000):
                    return btn
            except Exception:
                pass
        raise UnsupportedPayPalUIState("Continue/Weiter button not found")

    def _localize_amount(self, amount: str) -> str:
        """Convert a decimal-amount string to the locale separator used by PayPal."""
        page = self._require_page()
        try:
            input_value = page.locator("[data-nemo='amount']").first.input_value()
            separator = "," if "," in input_value else "."
        except Exception:
            separator = "," if self.locale == "de" else "."

        if separator == ",":
            return amount.replace(".", ",")
        return amount

    def _fill_amount_and_note(self, amount: str, note: str) -> None:
        page = self._require_page()
        amount_input = page.locator("[data-nemo='amount']").first
        amount_input.fill(self._localize_amount(amount))

        note_input = page.locator("[data-nemo='note-field']").first
        if note_input.is_visible(timeout=3000):
            note_input.fill(note)

        page.wait_for_timeout(1000)
        continue_btn = self._find_continue_button()
        continue_btn.click(force=True)
        page.wait_for_timeout(4000)

    def _select_balance_funding(self) -> None:
        page = self._require_page()
        # Wait for the funding source options.
        page.locator("input[type='radio'][name='fundingOption']").first.wait_for(state="visible", timeout=10000)

        # Prefer the option whose label text suggests PayPal balance.
        radios = page.locator("input[type='radio'][name='fundingOption']").all()
        selected_index = 0
        for idx, radio in enumerate(radios):
            try:
                label_text = self._radio_label_text(radio)
                if any(term in label_text.lower() for term in ["guthaben", "balance"]):
                    selected_index = idx
                    break
            except Exception:
                pass

        radios[selected_index].check(force=True)
        page.wait_for_timeout(1000)
        continue_btn = self._find_continue_button()
        continue_btn.click(force=True)
        page.wait_for_timeout(4000)

    def _radio_label_text(self, radio) -> str:
        page = self._require_page()
        # Try associated label or parent text.
        label_id = radio.get_attribute("aria-labelledby") or radio.get_attribute("id")
        if label_id:
            try:
                text = page.locator(f"[id='{label_id}']").first.inner_text(timeout=2000)
                if text:
                    return text
            except Exception:
                pass
        # Fallback to parent or surrounding text.
        parent = radio.locator("..")
        try:
            return parent.inner_text(timeout=2000)
        except Exception:
            return ""

    def _detect_fx(self, page: Page) -> bool:
        """Return True if the page shows currency conversion."""
        body = page.locator("body").inner_text().lower()
        return any(
            marker in body
            for marker in [
                "umrechnungskurs",
                "exchange rate",
                "währungsumrechnung",
                "currency conversion",
                "umgewandelt",
                "converted",
            ]
        )

    def _detect_payment_type(self, page: Page) -> tuple[bool, str]:
        """Detect whether a payment-type choice was presented and selected type."""
        text = page.locator("body").inner_text()
        lower = text.lower()
        choice_present = any(
            k in lower
            for k in [
                self._label("goods_and_services").lower(),
                self._label("friends_and_family").lower(),
                "goods and services",
                "friends and family",
            ]
        )
        if self._label("goods_and_services").lower() in lower or self._label("goods_and_services_alt").lower() in lower:
            return choice_present, "goods_and_services"
        if self._label("friends_and_family").lower() in lower:
            return choice_present, "friends_and_family"
        if choice_present:
            return choice_present, "unknown"
        return False, "automatic_business"

    def _extract_buyer_review_evidence(self, page: Page, amount: str, currency: str) -> dict[str, Any]:
        text = page.locator("body").inner_text()
        choice_present, payment_type = self._detect_payment_type(page)

        recipient_gets = ""
        match = re.search(r"[\d.,]+\s*[€$£¥]?\s*[A-Z]{3}", text)
        if match:
            recipient_gets = match.group(0)

        return {
            "payment_type_choice_present": choice_present,
            "payment_type_selected": payment_type,
            "recipient_gets": recipient_gets,
            "review_page_text_snippet": redact_text(text[:500]),
        }

    def _submit_payment(self) -> dict[str, Any]:
        page = self._require_page()
        send_btn = page.locator("button").filter(has_text=re.compile(r"^(Senden|Send)$", re.IGNORECASE)).first
        send_btn.wait_for(state="visible", timeout=10000)
        submitted_at = datetime.now(UTC).isoformat()
        send_btn.click(force=True)
        page.wait_for_timeout(8000)

        url = page.url
        self._validate_url(url)
        title = page.title()
        body_lower = page.locator("body").inner_text().lower()
        success = (
            "erfolg" in title.lower() or "success" in title.lower() or "sent" in body_lower or "gesendet" in body_lower
        )

        return {
            "success": success,
            "submitted_at": submitted_at,
            "final_url_path": urlparse(url).path,
            "page_title": redact_text(title),
        }

    def send_payment(
        self,
        buyer: Account,
        merchant: Account,
        amount: str,
        currency: str,
        note: str,
    ) -> dict[str, Any]:
        """Execute the full buyer-side send-money flow and return sanitized evidence."""
        self.login(buyer)
        page = self._require_page()
        try:
            self._select_recipient(merchant)
            self._fill_amount_and_note(amount, note)
            self._select_balance_funding()

            if self._detect_fx(page):
                return {
                    "status": ReconciliationStatus.EXCLUDED_FX_CASE.value,
                    "error": "Currency conversion detected",
                }

            review_evidence = self._extract_buyer_review_evidence(page, amount, currency)
            submit_result = self._submit_payment()

            if not submit_result.get("success"):
                return {
                    "status": ReconciliationStatus.BUYER_INTERACTION_BLOCKED.value,
                    "error": "Payment submission did not reach success state",
                }

            # Extract success page amount/currency.
            body = page.locator("body").inner_text()
            amount_match = re.search(r"([\d.,]+)\s*[€$£¥]?\s*([A-Z]{3})", body)
            sent_amount = amount_match.group(1) if amount_match else amount
            sent_currency = amount_match.group(2) if amount_match else currency

            return {
                "status": "submitted",
                "success": True,
                "sent_amount": sent_amount,
                "currency": sent_currency,
                "submitted_at": submit_result.get("submitted_at"),
                "payment_type_choice_present": review_evidence["payment_type_choice_present"],
                "payment_type_selected": review_evidence["payment_type_selected"],
                "funding_source": "paypal_balance",
                "review_evidence": review_evidence,
                "success_page_text_snippet": redact_text(body[:500]),
            }
        except (PlaywrightTimeoutError, UnsupportedPayPalUIState) as exc:
            return {
                "status": ReconciliationStatus.UNSUPPORTED_PAYPAL_UI_STATE.value,
                "error": str(exc),
            }
        finally:
            self.logout()

    def capture_failure_screenshot(self, path: Path) -> bool:
        page = self._require_page()
        if page.is_visible("input[type='password']", timeout=500):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        return path.exists()
