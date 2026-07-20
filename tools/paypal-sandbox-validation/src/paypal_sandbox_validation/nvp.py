"""PayPal NVP API client for Sandbox TransactionSearch/GetTransactionDetails.

Only ``api-3t.sandbox.paypal.com`` is allowed. Request bodies are URL-encoded and
never logged because they contain credentials. Responses are parsed into a
structured model and public reports mask correlation/transaction identifiers.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from paypal_sandbox_validation.accounts import Account

NVP_SANDBOX_HOST = "api-3t.sandbox.paypal.com"
NVP_SANDBOX_URL = f"https://{NVP_SANDBOX_HOST}/nvp"


class NVPError(Exception):
    pass


class NVPInvalidHostError(NVPError):
    pass


class NVPRequest:
    """Immutable NVP request payload without logging or string formatting."""

    def __init__(self, method: str, params: dict[str, str]) -> None:
        self.method = method
        self.params = params

    def to_body(self) -> bytes:
        return urllib.parse.urlencode(self.params).encode("utf-8")


class NVPResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ack: str = Field(default="", alias="ACK")
    correlation_id: str | None = Field(default=None, alias="CORRELATIONID")
    timestamp: str | None = Field(default=None, alias="TIMESTAMP")
    version: str | None = Field(default=None, alias="VERSION")
    build: str | None = Field(default=None, alias="BUILD")
    errors: list[dict[str, str]] = Field(default_factory=list)
    raw: dict[str, str] = Field(default_factory=dict)

    def error_message(self) -> str | None:
        if not self.errors:
            return None
        return "; ".join(
            f"{e.get('ERRORCODE', '?')}: {e.get('SHORTMESSAGE', '')} ({e.get('LONGMESSAGE', '')})"
            for e in self.errors
        )

    def is_success(self) -> bool:
        return self.ack.upper() in {"SUCCESS", "SUCCESSWITHWARNING"}

    def items(self) -> list[tuple[str, str]]:
        """Return list-indexed items for TransactionSearch-style responses."""
        return [(k, v) for k, v in self.raw.items() if not k.endswith("_errors")]

    def indexed_items(self, prefix: str) -> list[dict[str, str]]:
        """Collect indexed keys like L_TRANSACTIONID0 into a list of dicts."""
        pattern = re.compile(re.escape(prefix) + r"(\d+)$")
        groups: dict[int, dict[str, str]] = {}
        for key, value in self.raw.items():
            match = pattern.match(key)
            if match:
                idx = int(match.group(1))
                groups.setdefault(idx, {})[prefix] = value
        return [groups[i] for i in sorted(groups)]


def _parse_url_encoded(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True):
        result[key] = value
    return result


def _collect_errors(parsed: dict[str, str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    idx = 0
    while True:
        code = parsed.get(f"L_ERRORCODE{idx}")
        if code is None:
            break
        errors.append({
            "ERRORCODE": code,
            "SHORTMESSAGE": parsed.get(f"L_SHORTMESSAGE{idx}", ""),
            "LONGMESSAGE": parsed.get(f"L_LONGMESSAGE{idx}", ""),
        })
        idx += 1
    return errors


class PayPalNVPClient:
    """Thin, sandbox-only NVP client with explicit host validation."""

    def __init__(
        self,
        account: Account,
        endpoint: str = NVP_SANDBOX_URL,
        timeout: float = 30.0,
    ) -> None:
        if account.account_type.value != "BUSINESS":
            raise NVPError("NVP credentials are only available on Business accounts")
        if not account.nvp_user or not account.nvp_password or not account.nvp_signature:
            raise NVPError("Missing NVP credentials")

        parsed = urllib.parse.urlparse(endpoint)
        if parsed.hostname != NVP_SANDBOX_HOST:
            raise NVPInvalidHostError(f"NVP endpoint host not in allowlist: {endpoint}")

        self.account = account
        self.endpoint = endpoint
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PayPalNVPClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def call(self, method: str, **params: str) -> NVPResponse:
        """Call an NVP method. Request body is built but never logged."""
        payload = {
            "USER": self.account.nvp_user,
            "PWD": self.account.nvp_password,
            "SIGNATURE": self.account.nvp_signature,
            "VERSION": "204",
            "METHOD": method,
        }
        payload.update(params)

        body = urllib.parse.urlencode(payload).encode("utf-8")
        response = self._client.post(
            self.endpoint,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

        parsed = _parse_url_encoded(response.text)
        response_model = NVPResponse(
            raw=parsed,
            **_sanitize_nvp_response(parsed),
        )
        response_model.errors = _collect_errors(parsed)
        return response_model

    def transaction_search(
        self,
        start_date: str,
        end_date: str,
        email: str | None = None,
        amount: str | None = None,
        currency_code: str | None = None,
        status: str = "Success",
        transaction_class: str = "Received",
    ) -> NVPResponse:
        params: dict[str, str] = {
            "STARTDATE": start_date,
            "ENDDATE": end_date,
            "TRANSACTIONCLASS": transaction_class,
            "STATUS": status,
        }
        if email:
            params["EMAIL"] = email
        if amount:
            params["AMT"] = amount
        if currency_code:
            params["CURRENCYCODE"] = currency_code
        return self.call("TransactionSearch", **params)

    def get_transaction_details(self, transaction_id: str) -> NVPResponse:
        return self.call("GetTransactionDetails", TRANSACTIONID=transaction_id)


def _sanitize_nvp_response(parsed: dict[str, str]) -> dict[str, Any]:
    """Build a public-safe subset for the NVPResponse model."""
    return {
        "ACK": parsed.get("ACK", ""),
        "CORRELATIONID": parsed.get("CORRELATIONID"),
        "TIMESTAMP": parsed.get("TIMESTAMP"),
        "VERSION": parsed.get("VERSION"),
        "BUILD": parsed.get("BUILD"),
    }


def _parse_amount(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


def extract_transaction_search_results(response: NVPResponse) -> list[dict[str, str]]:
    """Return a list of transactions from a TransactionSearch response.

    Keys are normalized to a stable set. Transaction IDs are returned in the
    private field and should not be persisted publicly.
    """
    raw = response.raw
    transactions: list[dict[str, str]] = []
    index = 0
    while f"L_TRANSACTIONID{index}" in raw:
        transactions.append({
            "transaction_id": raw.get(f"L_TRANSACTIONID{index}", ""),
            "timestamp": raw.get(f"L_TIMESTAMP{index}", ""),
            "timezone": raw.get(f"L_TIMEZONE{index}", ""),
            "type": raw.get(f"L_TYPE{index}", ""),
            "email": raw.get(f"L_EMAIL{index}", ""),
            "name": raw.get(f"L_NAME{index}", ""),
            "transaction_class": raw.get(f"L_TRANSACTIONCLASS{index}", ""),
            "status": raw.get(f"L_STATUS{index}", ""),
            "amt": raw.get(f"L_AMT{index}", ""),
            "currency_code": raw.get(f"L_CURRENCYCODE{index}", ""),
            "fee_amt": raw.get(f"L_FEEAMT{index}", ""),
            "net_amt": raw.get(f"L_NETAMT{index}", ""),
        })
        index += 1
    return transactions


def _match_tx(
    tx: dict[str, str],
    amount: str,
    currency: str,
    buyer_email: str | None,
) -> bool:
    amount_dec = _parse_amount(amount)
    tx_amt = _parse_amount(tx.get("amt"))
    if tx_amt is None or amount_dec is None or tx_amt != amount_dec:
        return False
    if tx.get("currency_code") != currency:
        return False
    if tx.get("status") not in {"Success", "Completed"}:
        return False
    email = tx.get("email") or ""
    return not (buyer_email and email and email.lower() != buyer_email.lower())


def find_unique_transaction(
    response: NVPResponse,
    amount: str,
    currency: str,
    buyer_email: str | None = None,
) -> dict[str, Any]:
    """Identify a unique matching transaction or report the ambiguity."""
    if not response.is_success():
        return {
            "status": "nvp_search_failed",
            "error": response.error_message() or "TransactionSearch failed",
        }

    transactions = extract_transaction_search_results(response)
    matching = [tx for tx in transactions if _match_tx(tx, amount, currency, buyer_email)]

    if not matching:
        return {"status": "nvp_transaction_not_found", "error": "No matching transaction found"}
    if len(matching) > 1:
        return {
            "status": "nvp_transaction_ambiguous",
            "error": f"Multiple matching transactions: {len(matching)}",
        }

    return {"status": "found", "transaction": matching[0]}


def extract_transaction_details(response: NVPResponse) -> dict[str, Any] | None:
    """Parse a GetTransactionDetails response into a secret-free evidence dict."""
    if not response.is_success():
        return None
    raw = response.raw
    fee_amt = raw.get("FEEAMT")
    return {
        "transaction_type": raw.get("TRANSACTIONTYPE"),
        "payment_type": raw.get("PAYMENTTYPE"),
        "order_time": raw.get("ORDERTIME"),
        "amt": raw.get("AMT"),
        "fee_amt": fee_amt,
        "currency_code": raw.get("CURRENCYCODE"),
        "payment_status": raw.get("PAYMENTSTATUS"),
        "country_code": raw.get("COUNTRYCODE"),
        "exchange_rate": raw.get("EXCHANGERATE"),
        "settle_amt": raw.get("SETTLEAMT"),
        "settle_currency": raw.get("SETTLEAMT") and raw.get("CURRENCYCODE"),
        "note": raw.get("NOTE"),
        "invnum": raw.get("INVNUM"),
        "custom": raw.get("CUSTOM"),
        "has_fee": fee_amt is not None and fee_amt != "",
    }


def poll_for_unique_transaction(
    client: PayPalNVPClient,
    start_date: str,
    end_date: str,
    amount: str,
    currency: str,
    buyer_email: str | None = None,
    max_attempts: int = 6,
    delay_seconds: float = 5.0,
) -> dict[str, Any]:
    """Poll TransactionSearch until a unique result appears or the budget is exhausted."""
    result: dict[str, Any] = {
        "status": "nvp_transaction_not_found",
        "error": "No matching transaction found",
    }
    for _ in range(max_attempts):
        response = client.transaction_search(
            start_date=start_date,
            end_date=end_date,
            email=buyer_email,
            amount=amount,
            currency_code=currency,
        )
        result = find_unique_transaction(response, amount, currency, buyer_email)
        if result["status"] == "found":
            return result
        if result["status"] == "nvp_transaction_ambiguous":
            return result
        time.sleep(delay_seconds)
    return result


