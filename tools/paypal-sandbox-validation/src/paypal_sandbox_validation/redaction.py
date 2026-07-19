from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "password",
    "client_id",
    "secret",
    "access_token",
    "token",
    "refresh_token",
    "id_token",
    "authorization",
    "authorization_id",
    "auth",
    "cookie",
    "cookies",
    "session_id",
    "payer_id",
    "merchant_id",
    "account_id",
    "capture_id",
    "order_id",
    "email",
    "email_address",
    "primary_email_alias",
    "payment_card",
    "card_number",
    "bank_account",
    "bank_account_number",
    "routing_number",
    "iban",
    "bic",
    "address",
    "links",
}

CARD_BRANDS = {"visa", "mastercard", "amex", "discover", "jcb", "maestro"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\b")
ACCOUNT_ID_RE = re.compile(r"\b[A-Z0-9]{13}\b")


def mask_token(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


def mask_client_id(value: str, final_four: bool = True) -> str:
    if not value:
        return value
    if len(value) <= 8 or not final_four:
        return "***"
    return value[:4] + "..." + value[-4:]


def mask_password(value: str) -> str:
    return "***"


def mask_card(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return "*" * (len(digits) - 4) + digits[-4:]
    return "***"


def mask_email(value: str) -> str:
    if "@" not in value:
        return mask_token(value)
    user, domain = value.rsplit("@", 1)
    user = user[0] + "***" if len(user) > 1 else "***"
    return f"{user}@{domain}"


def mask_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    key_lower = key.lower()
    if key_lower in {"password"}:
        return mask_password(value)
    if key_lower in {"client_id"}:
        return mask_client_id(value)
    if key_lower in {"secret", "access_token", "token", "refresh_token", "id_token"}:
        return mask_token(value)
    if key_lower in {"payment_card", "card_number"}:
        return mask_card(value)
    if key_lower in {"email", "email_address", "primary_email_alias"}:
        return mask_email(value)
    if key_lower in {
        "bank_account",
        "bank_account_number",
        "routing_number",
        "iban",
        "bic",
    }:
        return "***"
    if key_lower in {"payer_id", "merchant_id", "account_id", "capture_id"}:
        return mask_token(value)
    if "link" in key_lower and ("href" in key_lower or value.startswith("http")):
        return mask_url(value)
    return value


def mask_url(value: str) -> str:
    try:
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
    except ImportError:
        return value
    parsed = urlparse(value)
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        for k in params:
            if k.lower() in SENSITIVE_KEYS or "token" in k.lower() or "id" in k.lower():
                params[k] = ["***"]
        query = urlencode(params, doseq=True)
        value = urlunparse(parsed._replace(query=query))
    return value


def redact_text(text: str) -> str:
    text = EMAIL_RE.sub(lambda m: mask_email(m.group(0)), text)
    # Generic tokens and long IDs are masked conservatively to avoid over-masking
    text = TOKEN_RE.sub(lambda m: mask_token(m.group(0)), text)
    return text


def sanitize_dict(data: Any, path: str = "") -> Any:
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            key_lower = key.lower()
            if key_lower in {"links", "href"} and isinstance(value, (str, list)):
                result[key] = sanitize_links(value)
                continue
            if key_lower in SENSITIVE_KEYS or any(
                token in key_lower for token in ("token", "secret", "password", "credential", "auth")
            ):
                if isinstance(value, str):
                    result[key] = mask_value(key, value)
                elif isinstance(value, (dict, list)):
                    result[key] = "***"
                else:
                    result[key] = value
                continue
            result[key] = sanitize_dict(value, f"{path}.{key}")
        return result
    if isinstance(data, list):
        return [sanitize_dict(item, path) for item in data]
    if isinstance(data, str):
        if path and any(
            token in path.lower() for token in ("token", "secret", "password", "credential", "auth", "email")
        ):
            return mask_value("", data)
        return data
    return data


def sanitize_links(links: Any) -> Any:
    if isinstance(links, list):
        return [sanitize_links(link) for link in links]
    if isinstance(links, dict):
        clean: dict[str, Any] = {}
        for k, v in links.items():
            if k.lower() in {"href", "url"} and isinstance(v, str):
                clean[k] = mask_url(v)
            else:
                clean[k] = v
        return clean
    if isinstance(links, str):
        return mask_url(links)
    return links


def sanitize_paypal_order(order: dict[str, Any]) -> dict[str, Any]:
    order = copy.deepcopy(order)
    if "id" in order and isinstance(order["id"], str):
        order["id"] = mask_token(order["id"])
    if "payer" in order and isinstance(order["payer"], dict):
        payer = order["payer"]
        if "email_address" in payer:
            payer["email_address"] = mask_email(payer["email_address"])
        if "payer_id" in payer:
            payer["payer_id"] = mask_token(payer["payer_id"])
    if "purchase_units" in order and isinstance(order["purchase_units"], list):
        for unit in order["purchase_units"]:
            if "payee" in unit and isinstance(unit["payee"], dict):
                payee = unit["payee"]
                if "email_address" in payee:
                    payee["email_address"] = mask_email(payee["email_address"])
                if "merchant_id" in payee:
                    payee["merchant_id"] = mask_token(payee["merchant_id"])
            if "payments" in unit and isinstance(unit["payments"], dict):
                payments = unit["payments"]
                for cap_list in payments.values():
                    if isinstance(cap_list, list):
                        for cap in cap_list:
                            if isinstance(cap, dict):
                                for k in ["id", "invoice_id", "custom_id"]:
                                    if k in cap and isinstance(cap[k], str):
                                        cap[k] = mask_token(cap[k])
    return sanitize_dict(order)


def sanitize_paypal_capture(capture: dict[str, Any]) -> dict[str, Any]:
    capture = copy.deepcopy(capture)
    if "id" in capture and isinstance(capture["id"], str):
        capture["id"] = mask_token(capture["id"])
    if "seller_receivable_breakdown" in capture and isinstance(capture["seller_receivable_breakdown"], dict):
        breakdown = capture["seller_receivable_breakdown"]
        for key in ["gross_amount", "paypal_fee", "net_amount", "receivable_amount", "exchange_rate"]:
            if key in breakdown and key == "exchange_rate":
                continue
    return sanitize_dict(capture)


def redact_exception(exc: Exception) -> str:
    msg = str(exc)
    for pattern in [EMAIL_RE, TOKEN_RE]:
        msg = pattern.sub(lambda m: "<redacted>", msg)
    return msg


def redact_path(value: str | None) -> str | None:
    if not value:
        return value
    return Path(value).name
