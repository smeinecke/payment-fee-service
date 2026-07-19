from __future__ import annotations

from urllib.parse import urlparse


class URLValidationError(ValueError):
    """Raised when a URL violates the sandbox allowlist."""


def _has_userinfo(parsed) -> bool:
    # urlparse puts user:pass in the netloc; username is set if userinfo is present.
    return parsed.username is not None or parsed.password is not None


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def validate_api_url(url: str) -> None:
    """Only https://api-m.sandbox.paypal.com/* is permitted."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise URLValidationError(f"PayPal API URL must use HTTPS: {url}")
    if parsed.hostname != "api-m.sandbox.paypal.com":
        raise URLValidationError(f"Only api-m.sandbox.paypal.com is allowed: {url}")
    port = parsed.port or _default_port(parsed.scheme)
    if port != 443:
        raise URLValidationError(f"PayPal API URL must use default HTTPS port: {url}")
    if _has_userinfo(parsed):
        raise URLValidationError(f"Userinfo is not allowed in PayPal API URL: {url}")


def validate_approval_url(url: str) -> None:
    """Only https://www.sandbox.paypal.com/* is permitted."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise URLValidationError(f"PayPal approval URL must use HTTPS: {url}")
    if parsed.hostname != "www.sandbox.paypal.com":
        raise URLValidationError(f"Only www.sandbox.paypal.com is allowed: {url}")
    if parsed.port is not None and parsed.port != 443:
        raise URLValidationError(f"PayPal approval URL must use default HTTPS port: {url}")
    if _has_userinfo(parsed):
        raise URLValidationError(f"Userinfo is not allowed in PayPal approval URL: {url}")


def validate_callback_url(url: str) -> None:
    """Only http://127.0.0.1:<port>/* is permitted."""
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise URLValidationError(f"Callback URL must use HTTP: {url}")
    if parsed.hostname != "127.0.0.1":
        raise URLValidationError(f"Callback URL must target 127.0.0.1: {url}")
    if parsed.port is None or not (1 <= parsed.port <= 65535):
        raise URLValidationError(f"Callback URL must use an explicit local port: {url}")
    if _has_userinfo(parsed):
        raise URLValidationError(f"Userinfo is not allowed in callback URL: {url}")
