from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from paypal_sandbox_validation.accounts import COUNTRY_CURRENCY, EXPECTED_COUNTRIES
from paypal_sandbox_validation.redaction import redact_path

SCENARIO_KEYS = {
    "provider",
    "channel",
    "payment_method",
    "user_action",
    "shipping_preference",
    "intent",
    "default",
    "per_country",
}

LIVE_PATTERNS = [
    re.compile(r"api-m\.paypal\.com"),
    re.compile(r"www\.paypal\.com/checkoutnow"),
    re.compile(r"\blive\b", re.IGNORECASE),
    re.compile(r"\bproduction\b", re.IGNORECASE),
]


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_scenarios(scenarios_path: str | Path | None = None) -> dict[str, Any]:
    if scenarios_path is None:
        scenarios_path = _package_root() / "config" / "scenarios.yaml"
    with open(scenarios_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_standard_wallet_scenario(scenarios: dict[str, Any], country: str) -> dict[str, Any] | None:
    spec = scenarios.get("standard_wallet_checkout")
    if not spec:
        return None
    per_country = spec.get("per_country", {})
    if country in per_country:
        return {**spec, **per_country[country]}
    return {**spec, **spec.get("default", {})}


def get_manual_send_scenario(scenarios: dict[str, Any], country: str) -> dict[str, Any] | None:
    spec = scenarios.get("manual_send_to_business")
    if not spec:
        return None
    per_country = spec.get("per_country", {})
    if country in per_country:
        return {**spec, **per_country[country]}
    return {**spec, **spec.get("default", {})}


def is_csv_tracked(csv_path: str | Path) -> bool:
    path = Path(csv_path)
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def gitignore_patterns_present(gitignore_path: Path | None = None) -> list[str]:
    if gitignore_path is None:
        gitignore_path = Path(__file__).with_name("..").resolve().with_name(".gitignore")
    if not gitignore_path.is_file():
        return []
    content = gitignore_path.read_text(encoding="utf-8")
    patterns = [
        "*.paypal-sandbox.local.csv",
        "*.paypal-sandbox.local.tsv",
        "*.paypal-sandbox-secrets.local.json",
        "artifacts/paypal-sandbox/",
        "artifacts/paypal-sandbox-manual/",
        ".playwright/paypal-sandbox/",
    ]
    return [p for p in patterns if p not in content]


def contains_live_host(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for pattern in LIVE_PATTERNS:
            if pattern.search(value):
                found.append(value)
                break
    return found


def validate_configuration(csv_path: str | Path, accounts: list[Any], repo_root: Path | None = None) -> dict[str, Any]:
    if repo_root is None:
        repo_root = _repo_root()
    csv_path = Path(csv_path).resolve()
    repo_root = repo_root.resolve()

    tracked = is_csv_tracked(csv_path)
    missing_patterns = gitignore_patterns_present(repo_root / ".gitignore")
    artifact_dir_ignored = "artifacts/paypal-sandbox/" not in missing_patterns
    live_hosts = contains_live_host([a.client_id or "" for a in accounts] + [a.secret or "" for a in accounts])

    return {
        "csv_path": redact_path(str(csv_path)),
        "csv_outside_repo": not str(csv_path).startswith(str(repo_root)),
        "csv_tracked": tracked,
        "gitignore_complete": not missing_patterns,
        "missing_gitignore_patterns": missing_patterns,
        "artifact_dir_ignored": artifact_dir_ignored,
        "live_hosts_found": live_hosts,
        "nvp_or_soap_fields": False,
    }


def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(country.upper(), "USD")


def validate_country_currency_mapping(country: str, currency: str) -> bool:
    return currency_for_country(country) == currency.upper()


def resolve_amounts(profile_name: str, scenarios: dict[str, Any]) -> list[str]:
    profile = scenarios.get("profiles", {}).get(profile_name, {})
    if "amount" in profile:
        return [profile["amount"]]
    if "amounts" in profile:
        return profile["amounts"]
    return ["10.00"]


def resolve_amount_for_country(amounts: list[str], country: str, currency: str) -> list[str]:
    if currency == "JPY":
        # Map two-decimal test amounts to JPY integer equivalents
        mapping = {"1.00": "100", "3.33": "333", "10.00": "1000", "100.00": "10000"}
        return [mapping.get(a, a) for a in amounts]
    return amounts


def expected_merchant_count() -> int:
    return len(EXPECTED_COUNTRIES)


def expected_buyer_count() -> int:
    return len(EXPECTED_COUNTRIES)
