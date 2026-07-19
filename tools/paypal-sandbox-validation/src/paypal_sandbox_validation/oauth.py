from __future__ import annotations

import base64
import contextlib
from typing import Any

import httpx

from paypal_sandbox_validation.models import OAuthProbeResult, OAuthProbeStatus
from paypal_sandbox_validation.paypal_api import PAYPAL_SANDBOX_API, require_sandbox_host


def _b64(credentials: tuple[str, str]) -> str:
    pair = f"{credentials[0]}:{credentials[1]}"
    return base64.b64encode(pair.encode()).decode()


def _classify_http_error(status: int, body: dict[str, Any] | None) -> OAuthProbeStatus:
    if body and isinstance(body, dict):
        error = str(body.get("error", "")).lower()
        if error == "invalid_client":
            return OAuthProbeStatus.INVALID_CLIENT
        if "auth" in error:
            return OAuthProbeStatus.AUTHENTICATION_FAILED
    if status in {400, 401}:
        return OAuthProbeStatus.AUTHENTICATION_FAILED
    if status in {403}:
        return OAuthProbeStatus.INVALID_CLIENT
    if status in {404, 500, 502, 503, 504}:
        return OAuthProbeStatus.UNREACHABLE
    return OAuthProbeStatus.UNEXPECTED


def request_token(client_id: str, secret: str, timeout: float = 30.0) -> tuple[str, dict[str, Any]]:
    """Request an OAuth token and return (token, token_response)."""
    require_sandbox_host(PAYPAL_SANDBOX_API)
    url = f"{PAYPAL_SANDBOX_API}/v1/oauth2/token"
    headers = {
        "Authorization": f"Basic {_b64((client_id, secret))}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = "grant_type=client_credentials"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, data=data)
    if response.status_code != 200:
        try:
            body = response.json()
        except Exception:
            body = None
        status = _classify_http_error(response.status_code, body)
        raise OAuthError(f"OAuth probe failed: {status.value} (HTTP {response.status_code})", status=status)
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError("OAuth response missing access_token", status=OAuthProbeStatus.UNEXPECTED)
    return token, payload


class OAuthError(Exception):
    def __init__(self, message: str, status: OAuthProbeStatus | None = None) -> None:
        super().__init__(message)
        self.status = status or OAuthProbeStatus.UNEXPECTED


class OAuthCache:
    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def get(self, country: str) -> str | None:
        return self._tokens.get(country)

    def set(self, country: str, token: str) -> None:
        self._tokens[country] = token

    def clear(self) -> None:
        self._tokens.clear()


def probe_credentials(client_id: str, secret: str, country: str) -> OAuthProbeResult:
    """Probe credentials without retaining the token."""
    try:
        token, payload = request_token(client_id, secret)
    except OAuthError as exc:
        return OAuthProbeResult(country=country, status=exc.status, classification=exc.status.value)
    except httpx.TimeoutException:
        return OAuthProbeResult(country=country, status=OAuthProbeStatus.TIMEOUT)
    except httpx.NetworkError:
        return OAuthProbeResult(country=country, status=OAuthProbeStatus.UNREACHABLE)
    except Exception:
        return OAuthProbeResult(country=country, status=OAuthProbeStatus.UNEXPECTED)
    finally:
        # Ensure the token is not retained after the probe.
        with contextlib.suppress(NameError):
            del token

    scope = payload.get("scope", "")
    scope_count = len(scope.split()) if isinstance(scope, str) else 0
    return OAuthProbeResult(
        country=country,
        status=OAuthProbeStatus.SUCCESS,
        expires_in=payload.get("expires_in"),
        scope_count=scope_count,
    )


def fetch_token(cache: OAuthCache, client_id: str, secret: str, country: str) -> str:
    cached = cache.get(country)
    if cached:
        return cached
    token, _ = request_token(client_id, secret)
    cache.set(country, token)
    return token
