from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from payment_fee import PaymentFeeEngine
from payment_fee.providers.paypal.provider import PayPalProvider

from paypal_sandbox_validation.configuration import (
    get_manual_send_scenario,
    get_standard_wallet_scenario,
    load_scenarios,
)

# Broad payer-region groupings used by the PayPal fee dataset.
EEA_COUNTRIES = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FR",
    "GR",
    "HR",
    "HU",
    "IE",
    "IS",
    "IT",
    "LI",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "NO",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "CH",
}
US_CA_COUNTRIES = {"US", "CA", "MX"}


def _data_path() -> str:
    candidate = Path(__file__).resolve().parents[4] / "paypal-fee-data"
    if candidate.is_dir():
        return str(candidate)
    env = __import__("os").environ.get("PAYPAL_FEE_DATA_PATH")
    if env:
        return env
    raise FileNotFoundError("PayPal fee data directory not found. Set PAYPAL_FEE_DATA_PATH.")


class QuoteAdapter:
    def __init__(self, data_path: str | None = None) -> None:
        self.data_path = data_path or _data_path()
        self.engine = PaymentFeeEngine.from_paths(paypal=self.data_path)
        self.scenarios = load_scenarios()

    def resolve_scenario(self, merchant_country: str) -> dict[str, Any] | None:
        return self._resolve_scenario_for(merchant_country, get_standard_wallet_scenario)

    def resolve_manual_scenario(self, merchant_country: str) -> dict[str, Any] | None:
        return self._resolve_scenario_for(
            merchant_country,
            get_manual_send_scenario,
            preferred=["other_commercial", "goods_and_services", "paypal_checkout"],
        )

    def _resolve_scenario_for(
        self,
        merchant_country: str,
        scenario_getter,
        preferred: list[str] | None = None,
    ) -> dict[str, Any] | None:
        spec = scenario_getter(self.scenarios, merchant_country)
        if not spec:
            return None
        cap = self.engine.capabilities("paypal", merchant_country)
        product_id = spec.get("product_id")
        variant_id = spec.get("variant_id")
        if product_id and variant_id:
            variants = cap.calculable_products.get(product_id, [])
            if variant_id in variants:
                return spec
        # Fallback to a calculable wallet-like product/variant.
        products = preferred or ["paypal_checkout", "other_commercial", "goods_and_services"]
        for product in products:
            variants = cap.calculable_products.get(product, [])
            if "standard" in variants:
                return {**spec, "product_id": product, "variant_id": "standard"}
            if variants:
                return {**spec, "product_id": product, "variant_id": variants[0]}
        return None

    def _provider(self) -> PayPalProvider:
        return self.engine._registry.get("paypal")  # type: ignore[attr-defined]

    def _country_derived(self, merchant_country: str) -> Any:
        provider = self._provider()
        return provider._countries[merchant_country.upper()].derived

    def _rule_for_product(self, merchant_country: str, product_id: str, variant_id: str) -> list[Any]:
        derived = self._country_derived(merchant_country)
        rules = []
        for rule in derived.transaction_fee_rules:
            if rule.id != product_id:
                continue
            if variant_id and (rule.variant_id or "default") != variant_id:
                continue
            rules.append(rule)
        return rules

    def build_quote_manual(
        self,
        merchant_country: str,
        buyer_country: str,
        amount: str,
        currency: str,
    ) -> dict[str, Any]:
        scenario = self.resolve_manual_scenario(merchant_country)
        return self._build_quote(
            merchant_country=merchant_country,
            buyer_country=buyer_country,
            amount=amount,
            currency=currency,
            scenario=scenario,
        )

    def build_quote(
        self,
        merchant_country: str,
        buyer_country: str,
        amount: str,
        currency: str,
        product_id: str | None = None,
        variant_id: str | None = None,
    ) -> dict[str, Any]:
        if product_id and variant_id:
            scenario = {
                "provider": "paypal",
                "channel": "online",
                "payment_method": "paypal_wallet",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "intent": "CAPTURE",
                "product_id": product_id,
                "variant_id": variant_id,
            }
        else:
            scenario = self.resolve_scenario(merchant_country)
        return self._build_quote(
            merchant_country=merchant_country,
            buyer_country=buyer_country,
            amount=amount,
            currency=currency,
            scenario=scenario,
        )

    def _build_quote(
        self,
        merchant_country: str,
        buyer_country: str,
        amount: str,
        currency: str,
        scenario: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not scenario:
            raise QuoteResolutionError(
                f"No calculable standard wallet scenario for {merchant_country}",
                status="account_capability_unavailable",
            )
        product_id = scenario["product_id"]
        variant_id = scenario["variant_id"]

        validate_amount_for_currency(amount, currency)

        transaction_region = self._resolve_transaction_region(merchant_country, buyer_country, product_id, variant_id)
        payer_region = self._resolve_payer_region(
            merchant_country, buyer_country, product_id, variant_id, transaction_region
        )

        request_dict: dict[str, Any] = {
            "provider": "paypal",
            "amount": {"value": amount, "currency": currency},
            "account_country": merchant_country,
            "customer_country": buyer_country,
            "settlement_currency": currency,
            "transaction": {
                "product_id": product_id,
                "variant_id": variant_id,
                "payment_method": scenario.get("payment_method", "paypal_wallet"),
                "channel": scenario.get("channel", "online"),
                "transaction_region": transaction_region,
                "payer_region": payer_region,
                "funding_source": "paypal_balance",
            },
        }

        try:
            response = self.engine.quote(request_dict)
        except Exception as exc:
            raise QuoteResolutionError(
                f"Library could not calculate fee: {exc}",
                status=_map_library_error(exc),
            ) from exc

        quote_data = json.loads(response.model_dump_json())
        quote_data["_scenario"] = scenario
        quote_data["_request"] = request_dict
        quote_data["_data_path"] = self.data_path
        self.enrich_quote_metadata(quote_data)
        return quote_data

    def enrich_quote_metadata(self, quote: dict[str, Any]) -> dict[str, Any]:
        """Attach schedule and component metadata to a quote for reporting and reconciliation."""
        request = quote.get("_request", {})
        transaction = request.get("transaction", {})
        merchant_country = request.get("account_country", "")
        product_id = quote.get("_scenario", {}).get("product_id")
        variant_id = quote.get("_scenario", {}).get("variant_id") or "default"
        components = quote.get("components", []) or []

        processing = next((c for c in components if c.get("type") == "processing"), {})
        surcharge = next((c for c in components if c.get("type") == "surcharge"), {})
        base_rule_id = processing.get("source_rule_id")

        fixed_fee_schedule_id: str | None = None
        international_surcharge_schedule_id: str | None = None
        if merchant_country and product_id:
            for rule in self._rule_for_product(merchant_country, product_id, variant_id):
                # Use the rule that defines the base percentage / fixed fee schedule.
                if rule.fixed_fee_schedule:
                    fixed_fee_schedule_id = rule.fixed_fee_schedule
                if rule.international_surcharge_schedule:
                    international_surcharge_schedule_id = rule.international_surcharge_schedule
                if not base_rule_id and rule.id:
                    base_rule_id = f"paypal:{merchant_country}:{rule.id}:{rule.variant_id or 'default'}:base"

        metadata = {
            "base_rule_id": base_rule_id,
            "fixed_fee_schedule_id": fixed_fee_schedule_id,
            "international_surcharge_schedule_id": international_surcharge_schedule_id,
            "base_percentage": processing.get("rate_percentage"),
            "fixed_amount": processing.get("fixed_amount"),
            "surcharge_percentage": surcharge.get("rate_percentage"),
            "predicted_total_fee": quote.get("processing_fee", {}).get("value"),
            "payer_region": transaction.get("payer_region"),
            "component_signature": [(c.get("type"), c.get("amount")) for c in components],
        }
        quote["_schedule_metadata"] = metadata
        return metadata

    def _resolve_transaction_region(
        self, merchant_country: str, buyer_country: str, product_id: str, variant_id: str
    ) -> str:
        rules = self._rule_for_product(merchant_country, product_id, variant_id)
        explicit = {r.conditions.get("transaction_region") for r in rules if "transaction_region" in r.conditions}
        if explicit == {"domestic"}:
            return "domestic"
        if explicit == {"international"}:
            return "international"
        if merchant_country.upper() == buyer_country.upper():
            return "domestic"
        return "international"

    def _resolve_payer_region(
        self,
        merchant_country: str,
        buyer_country: str,
        product_id: str,
        variant_id: str,
        transaction_region: str,
    ) -> str | None:
        if transaction_region == "domestic" and merchant_country.upper() == buyer_country.upper():
            # Provide the buyer country; if it is not a literal region label in the
            # surcharge schedule, the engine returns None and adds no surcharge.
            return buyer_country.upper()

        schedule_regions = self._list_schedule_regions(merchant_country, product_id, variant_id)
        if not schedule_regions:
            return buyer_country.upper()

        region = _country_to_region(buyer_country, schedule_regions)
        return region

    def _list_schedule_regions(self, merchant_country: str, product_id: str, variant_id: str) -> set[str]:
        derived = self._country_derived(merchant_country)
        rules = self._rule_for_product(merchant_country, product_id, variant_id)
        regions: set[str] = set()
        for rule in rules:
            schedule_name = rule.international_surcharge_schedule
            if not schedule_name:
                for comp in rule.fee_components:
                    if comp.type == "international_surcharge_schedule" and comp.schedule_id:
                        schedule_name = comp.schedule_id
            if schedule_name and schedule_name in derived.international_surcharge_schedules:
                schedule = derived.international_surcharge_schedules[schedule_name]
                regions.update(e.payer_region for e in schedule.entries)
        return regions


def _country_to_region(country: str, schedule_regions: set[str]) -> str | None:
    country = country.upper()
    if "GB" in schedule_regions and country == "GB":
        return "GB"
    if "US_CA" in schedule_regions and country in US_CA_COUNTRIES:
        return "US_CA"
    if "EEA" in schedule_regions and country in EEA_COUNTRIES:
        return "EEA"
    if "EUROPE_I" in schedule_regions and country in EEA_COUNTRIES:
        return "EUROPE_I"
    if "EUROPE_II" in schedule_regions and country in EEA_COUNTRIES:
        return "EUROPE_II"
    if "NORTHERN_EUROPE" in schedule_regions and country in {"DK", "FI", "IS", "NO", "SE"}:
        return "NORTHERN_EUROPE"
    if "OTHER" in schedule_regions:
        return "OTHER"
    if schedule_regions:
        return country
    return None


def _map_library_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name in {"InsufficientTransactionContext"}:
        return "library_missing_context"
    if name in {"AmbiguousFeeRules"}:
        return "library_ambiguous"
    return "library_not_calculable"


class QuoteResolutionError(Exception):
    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


ZERO_DECIMAL_CURRENCIES = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}


def validate_amount_for_currency(amount: str, currency: str) -> None:
    """Reject fractional amounts for zero-decimal currencies such as JPY."""
    if currency.upper() in ZERO_DECIMAL_CURRENCIES:
        dec = Decimal(str(amount))
        if dec != dec.to_integral_value():
            raise ValueError(f"{currency} amount {amount} must be an integer")


def minor_units(value: Decimal | str, currency: str) -> int:
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if currency in ZERO_DECIMAL_CURRENCIES:
        return int(dec)
    if currency in {"BHD", "JOD", "KWD", "OMR", "TND"}:
        return int(dec * Decimal("1000"))
    return int(dec * Decimal("100"))
