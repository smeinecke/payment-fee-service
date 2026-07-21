from __future__ import annotations

import contextlib

from playwright.sync_api import Page


def _fill_paypal_login_form(page: Page, email: str, password: str) -> None:
    """Fill the PayPal Sandbox login form on the current page.

    Handles both single-page and split email-then-password flows.
    """
    email_input = page.locator("input[name='login_email'], input#email, input[type='email']").first
    if email_input.is_visible(timeout=10000):
        email_input.fill(email)
        next_btn = page.locator("button#btnNext").first
        if next_btn.is_visible(timeout=5000):
            next_btn.click()
            with contextlib.suppress(Exception):
                page.locator("div.transitioning.spinner").wait_for(state="hidden", timeout=15000)

    pw_input = page.locator("input[name='login_password'], input#password, input[type='password']").first
    pw_input.wait_for(state="visible", timeout=15000)
    pw_input.fill(password)
    page.locator("button#btnLogin").first.click()
