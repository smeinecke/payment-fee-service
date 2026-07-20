from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator

from paypal_sandbox_validation.manual_browser import ManualPaymentBrowser
from paypal_sandbox_validation.models import Account, QualificationStatus
from paypal_sandbox_validation.quote_adapter import currency_exponent, minor_units

PROFILE_PAGE_PATH = "merchantapps/businesstools/acceptpayments/checkout"
ALLOWED_HOSTS = frozenset({"www.sandbox.paypal.com"})
ALLOWED_CARD_QUALIFIERS = frozenset({"starting_at"})


class PricingLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percentage: str
    fixed: dict[str, str]
    qualifier: str | None = None

    @field_validator("fixed")
    @classmethod
    def _fixed_has_value_and_currency(cls, v: dict[str, str]) -> dict[str, str]:
        if "value" not in v or "currency" not in v:
            raise ValueError("fixed must contain 'value' and 'currency'")
        return v


class SandboxProfilePricing(BaseModel):
    """Secret-free evidence from an authenticated Sandbox Business profile page."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "paypal"
    environment: str = "sandbox"
    merchant_country: str
    evidence_type: str = "sandbox_profile_pricing"
    pricing_source: str = "sandbox_merchant_profile"
    profile_page: str = PROFILE_PAGE_PATH
    profile_observed_at: str | None = None
    wallet: PricingLine
    card: PricingLine
    international_surcharge: dict[str, Any] | None = None
    screenshot_sha256: str | None = None
    contains_account_identifiers: bool = False


class ProfilePricingVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_country: str
    profile_wallet_formula: str
    observed_wallet_formula: str | None = None
    delta_minor_units: int | None = None
    status: str
    production_representative: bool = False
    international_surcharge_note: str | None = None


def _validate_url(url: str) -> None:
    host = urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Navigation to non-Sandbox PayPal host rejected: {url}")


def _parse_decimal(value: str, name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc


def _validate_money(value: str, currency: str, name: str) -> None:
    d = _parse_decimal(value, name)
    if d < 0:
        raise ValueError(f"{name} must be non-negative: {value}")
    exp = currency_exponent(currency)
    quantized = d.quantize(Decimal(1) / (Decimal(10) ** exp))
    if d != quantized:
        raise ValueError(f"{name} has too many decimal places for {currency}: {value}")


def validate_profile_pricing_input(
    merchant_country: str,
    wallet_percentage: str,
    wallet_fixed: str,
    wallet_currency: str,
    card_percentage: str,
    card_fixed: str,
    card_currency: str,
    card_qualifier: str | None = None,
) -> SandboxProfilePricing:
    """Validate raw CLI input and return a SandboxProfilePricing model."""
    wallet_pct = _parse_decimal(wallet_percentage, "wallet percentage")
    if wallet_pct < 0:
        raise ValueError("wallet percentage must be non-negative")
    _validate_money(wallet_fixed, wallet_currency, "wallet fixed fee")

    card_pct = _parse_decimal(card_percentage, "card percentage")
    if card_pct < 0:
        raise ValueError("card percentage must be non-negative")
    _validate_money(card_fixed, card_currency, "card fixed fee")

    if card_qualifier is not None and card_qualifier not in ALLOWED_CARD_QUALIFIERS:
        raise ValueError(f"Unsupported card qualifier: {card_qualifier}")

    return SandboxProfilePricing(
        merchant_country=merchant_country.upper(),
        wallet=PricingLine(percentage=wallet_percentage, fixed={"value": wallet_fixed, "currency": wallet_currency}),
        card=PricingLine(
            percentage=card_percentage,
            fixed={"value": card_fixed, "currency": card_currency},
            qualifier=card_qualifier,
        ),
    )


def _wallet_formula_string(line: PricingLine) -> str:
    pct = line.percentage
    fixed = line.fixed["value"]
    currency = line.fixed["currency"]
    return f"{pct}% + {currency} {fixed}"


def _extract_rate(text: str) -> tuple[str, str, str] | None:
    """Extract (percentage, currency, fixed) from a profile pricing text block."""
    # Match patterns like "1.90% + EUR 0.35" or "Starting at 2.99% + EUR 0.39".
    match = re.search(r"(\d+(?:\.\d+)?)%\s*\+\s*([A-Z]{3})\s*(\d+(?:\.\d+)?)", text)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def _section_text(text: str, start_heading: str, end_headings: tuple[str, ...]) -> str:
    """Return the text between a start heading and the next end heading."""
    start = text.lower().find(start_heading.lower())
    if start == -1:
        return ""
    end = -1
    for heading in end_headings:
        idx = text.lower().find(heading.lower(), start + len(start_heading))
        if idx != -1 and (end == -1 or idx < end):
            end = idx
    return text[start:end] if end != -1 else text[start:]


def extract_profile_pricing_from_html(
    html: str,
    merchant_country: str,
    wallet_currency: str,
    card_currency: str,
) -> SandboxProfilePricing:
    """Parse a Sandbox profile pricing page into a SandboxProfilePricing model.

    The parser is fail-closed: if the expected semantic headings or rate
    patterns are missing, it raises ValueError.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    wallet_headings = ("when customers pay with paypal",)
    card_headings = ("when customers pay with credit or debit card",)

    wallet_section = _section_text(text, wallet_headings[0], card_headings)
    card_section = _section_text(text, card_headings[0], ())

    if not wallet_section or not card_section:
        raise ValueError("Profile page headings not found; layout is unsupported.")

    wallet_rate = _extract_rate(wallet_section)
    card_rate = _extract_rate(card_section)

    if not wallet_rate:
        raise ValueError("Could not extract wallet pricing from profile page.")
    if not card_rate:
        raise ValueError("Could not extract card pricing from profile page.")

    wallet_pct, wallet_currency_extracted, wallet_fixed = wallet_rate
    card_pct, card_currency_extracted, card_fixed = card_rate

    if wallet_currency_extracted != wallet_currency:
        raise ValueError(f"Wallet currency mismatch: expected {wallet_currency}, got {wallet_currency_extracted}")
    if card_currency_extracted != card_currency:
        raise ValueError(f"Card currency mismatch: expected {card_currency}, got {card_currency_extracted}")

    card_qualifier = "starting_at" if "starting at" in card_section.lower() else None

    return SandboxProfilePricing(
        merchant_country=merchant_country,
        wallet=PricingLine(percentage=wallet_pct, fixed={"value": wallet_fixed, "currency": wallet_currency}),
        card=PricingLine(
            percentage=card_pct,
            fixed={"value": card_fixed, "currency": card_currency},
            qualifier=card_qualifier,
        ),
    )


class ProfilePricingBrowser(ManualPaymentBrowser):
    """Playwright wrapper for read-only inspection of Sandbox profile pricing."""

    _ALLOWED_HOSTS = ALLOWED_HOSTS

    def inspect(
        self,
        account: Account,
        merchant_country: str,
        wallet_currency: str,
        card_currency: str,
    ) -> SandboxProfilePricing:
        """Log in and read the authenticated profile pricing page."""
        self.login(account)
        url = f"https://www.sandbox.paypal.com/{PROFILE_PAGE_PATH}"
        _validate_url(url)
        page = self._require_page()
        page.goto(url, timeout=120000)
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        html = page.content()
        return extract_profile_pricing_from_html(html, merchant_country, wallet_currency, card_currency)


def record_profile_pricing(
    registry: dict[str, Any],
    evidence: SandboxProfilePricing,
    *,
    set_manual_send: bool = False,
    set_orders_v2: bool = False,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Store secret-free profile pricing evidence in the qualification registry."""
    if observed_at:
        evidence.profile_observed_at = observed_at
    elif not evidence.profile_observed_at:
        evidence.profile_observed_at = datetime.now(UTC).isoformat()

    merchant = evidence.merchant_country
    entry = registry.setdefault(merchant, {})
    entry["merchant_country"] = merchant
    entry["sandbox_profile_pricing"] = evidence.model_dump(exclude_none=False)
    entry["representative_for_public_rates"] = False

    status_value = QualificationStatus.SANDBOX_PROFILE_PRICING_CONFIRMED.value
    if set_manual_send:
        entry["manual_send_to_business"] = status_value
    if set_orders_v2:
        entry["orders_v2_checkout"] = status_value

    return entry


def _infer_observed_wallet_formula(entry: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (percentage, fixed_value, currency) from observed transaction evidence."""
    observed_sources = [
        entry.get("observed_account_formula"),
        (entry.get("manual_send_observation") or {}).get("observed_account_formula"),
    ]
    for observed in observed_sources:
        if not observed:
            continue
        pct = observed.get("percentage")
        fixed = observed.get("fixed", {})
        value = fixed.get("value")
        currency = fixed.get("currency")
        if pct and value and currency:
            return pct, value, currency

    # Fall back to fitting a linear formula from domestic observations.
    domestic = [o for o in entry.get("observations", []) if o.get("buyer_country") == entry.get("merchant_country")]
    if len(domestic) < 2:
        return None
    try:
        amounts = sorted((Decimal(o["amount"]), Decimal(o["paypal_fee"])) for o in domestic)
        (a1, f1), (a2, f2) = amounts[0], amounts[-1]
        if a1 == a2:
            return None
        slope = (f2 - f1) / (a2 - a1)
        fixed = f1 - slope * a1
        currency = domestic[0].get("currency", "")
        # Round fixed to currency minor units.
        exp = currency_exponent(currency)
        fixed = fixed.quantize(Decimal(1) / (Decimal(10) ** exp))
        return str(slope * Decimal("100")), str(fixed), currency
    except Exception:
        return None


def verify_profile_against_transactions(registry: dict[str, Any], merchant_country: str) -> ProfilePricingVerification:
    """Compare recorded Sandbox profile pricing against observed transaction pricing."""
    entry = registry.get(merchant_country, {})
    profile = entry.get("sandbox_profile_pricing")
    observed = _infer_observed_wallet_formula(entry)
    representative = bool(entry.get("representative_for_public_rates"))

    if not profile:
        return ProfilePricingVerification(
            merchant_country=merchant_country,
            profile_wallet_formula="",
            observed_wallet_formula=None,
            status="profile_evidence_missing",
            production_representative=representative,
        )

    wallet = profile["wallet"]
    profile_formula = _wallet_formula_string(PricingLine.model_validate(wallet))

    if not observed:
        return ProfilePricingVerification(
            merchant_country=merchant_country,
            profile_wallet_formula=profile_formula,
            observed_wallet_formula=None,
            status="transaction_evidence_missing",
            production_representative=representative,
        )

    observed_pct, observed_fixed, observed_currency = observed
    observed_formula = f"{observed_pct}% + {observed_currency} {observed_fixed}"

    profile_pct = Decimal(wallet["percentage"])
    profile_fixed = Decimal(wallet["fixed"]["value"])
    profile_currency = wallet["fixed"]["currency"]

    # Compare at a common amount (10.00 units) using real minor units.
    amount = Decimal("10.00")
    profile_fee = profile_pct / Decimal("100") * amount + profile_fixed
    observed_fee = Decimal(observed_pct) / Decimal("100") * amount + Decimal(observed_fixed)
    delta = minor_units(str(profile_fee), profile_currency) - minor_units(str(observed_fee), observed_currency)

    status = "profile_matches_transactions" if delta == 0 else "profile_transaction_mismatch"

    surcharge_note = None
    surcharge = entry.get("international_surcharge")
    profile = entry.get("sandbox_profile_pricing") or {}
    if isinstance(profile, dict) and profile.get("international_surcharge"):
        surcharge = profile["international_surcharge"]
    if merchant_country == "AU" and surcharge:
        surcharge_note = (
            f"tested international surcharge: +{surcharge.get('percentage_points')}pp "
            f"({surcharge.get('confirmed_case', '')})"
        )

    return ProfilePricingVerification(
        merchant_country=merchant_country,
        profile_wallet_formula=profile_formula,
        observed_wallet_formula=observed_formula,
        delta_minor_units=delta,
        status=status,
        production_representative=representative,
        international_surcharge_note=surcharge_note,
    )


def build_profile_pricing_verifications(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return profile/transaction verifications for every merchant with profile evidence."""
    results = []
    for merchant in sorted(registry):
        if "sandbox_profile_pricing" in registry[merchant]:
            results.append(verify_profile_against_transactions(registry, merchant).model_dump())
    return results
