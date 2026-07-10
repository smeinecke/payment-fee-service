from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from payment_fee_service.data.snapshots import ProviderSnapshot
from payment_fee_service.domain.errors import (
    InsufficientContextError,
    QuoteUnavailableError,
    UnknownMarketError,
)
from payment_fee_service.domain.models import (
    CapabilityInfo,
    MarketInfo,
    PayPalQuoteRequest,
    PayPalTransactionType,
    QuoteRequest,
)
from payment_fee_service.domain.rules import CompiledFeePlan, ExecutableFeeRule

CATEGORY_FIELDS = {
    PayPalTransactionType.STANDARD_COMMERCIAL: "standard_commercial",
    PayPalTransactionType.GOODS_AND_SERVICES: "goods_and_services",
    PayPalTransactionType.MICROPAYMENTS: "micropayments",
    PayPalTransactionType.DONATIONS: "donations",
    PayPalTransactionType.NONPROFIT: "nonprofit",
}


class PayPalProvider:
    provider_id = "paypal"

    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot
        self._countries = {
            str(
                item.get("iso_country_code")
                or item.get("country_code")
                or item.get("paypal_market_code")
            ).upper(): item
            for item in snapshot.core.get("countries", [])
        }
        self._index = {
            str(
                item.get("iso_country_code")
                or item.get("country_code")
                or item.get("paypal_market_code")
            ).upper(): item
            for item in snapshot.index.get("countries", [])
        }

    def compile_rules(self, request: QuoteRequest) -> CompiledFeePlan:
        if not isinstance(request, PayPalQuoteRequest):
            raise TypeError("PayPalProvider received a non-PayPal request")
        market = self._countries.get(request.account_country)
        if market is None:
            raise UnknownMarketError(self.provider_id, request.account_country)
        derived = market.get("derived") or {}
        field = CATEGORY_FIELDS[request.payment.transaction_type]
        commercial_fee = derived.get(field)
        if not isinstance(commercial_fee, dict) or commercial_fee.get("percentage") is None:
            raise QuoteUnavailableError(
                "The requested PayPal transaction category is not classified for this market.",
                market=request.account_country,
                transaction_type=request.payment.transaction_type,
            )

        percentage = self._decimal(commercial_fee.get("percentage"), "percentage")
        fixed_reference = commercial_fee.get("fixed_fee_reference")
        if not fixed_reference:
            raise QuoteUnavailableError(
                "The selected PayPal fee category has no fixed-fee reference.",
                market=request.account_country,
                transaction_type=request.payment.transaction_type,
            )
        fixed_fees = derived.get(str(fixed_reference)) or []
        if not isinstance(fixed_fees, list):
            raise QuoteUnavailableError(
                "The selected PayPal fixed-fee reference is unsupported.",
                market=request.account_country,
                fixed_fee_reference=fixed_reference,
            )
        fixed = next(
            (
                item
                for item in fixed_fees
                if str(item.get("currency", "")).upper() == request.amount.currency
            ),
            None,
        )
        if fixed is None:
            raise QuoteUnavailableError(
                "No PayPal fixed fee is published for the transaction currency.",
                market=request.account_country,
                currency=request.amount.currency,
            )

        source = self._index.get(request.account_country, {})
        source_url = source.get("source_url")
        rules = [
            ExecutableFeeRule(
                rule_id=f"paypal:{request.account_country}:{field}",
                label=f"PayPal {request.payment.transaction_type.value.replace('_', ' ')} fee",
                percentage=percentage,
                fixed_amount=self._decimal(fixed.get("amount"), "fixed fee"),
                fixed_currency=request.amount.currency,
                source_url=source_url,
                classification_status="classified",
                exactness="exact",
                confidence=1.0,
            )
        ]
        assumptions = [
            "Public standard pricing was used; negotiated merchant pricing is not represented.",
            "The published dataset does not encode provider settlement rounding, so "
            "standard currency rounding is used.",
        ]

        if request.customer_country and request.customer_country != request.account_country:
            surcharge = self._resolve_surcharge(derived, request)
            if surcharge is not None:
                rules.append(
                    ExecutableFeeRule(
                        rule_id=f"paypal:{request.account_country}:international:{surcharge['region']}",
                        label=f"PayPal international surcharge ({surcharge['region']})",
                        percentage=self._decimal(surcharge.get("percentage_points"), "surcharge"),
                        source_url=source_url,
                        classification_status="classified",
                        exactness="exact",
                        confidence=1.0,
                    )
                )

        return CompiledFeePlan(
            provider=self.provider_id,
            market=request.account_country,
            currency=request.amount.currency,
            rules=rules,
            assumptions=assumptions,
            schema_version=self.snapshot.schema_version,
            content_sha256=source.get("content_sha256"),
            source_urls=[source_url] if source_url else [],
            source_updated_at=source.get("source_updated_at"),
            data_ref=self.snapshot.data_ref,
        )

    def _resolve_surcharge(
        self,
        derived: dict[str, Any],
        request: PayPalQuoteRequest,
    ) -> dict[str, Any] | None:
        surcharges = [
            item
            for item in (derived.get("international_surcharges") or [])
            if item.get("percentage_points") is not None
        ]
        if not surcharges:
            return None
        region = request.payment.surcharge_region
        if region:
            match = next(
                (item for item in surcharges if str(item.get("region", "")).upper() == region),
                None,
            )
            if match is None:
                raise QuoteUnavailableError(
                    "The requested PayPal surcharge region is not published for this market.",
                    market=request.account_country,
                    surcharge_region=region,
                    available_regions=[item.get("region") for item in surcharges],
                )
            return match
        direct = next(
            (
                item
                for item in surcharges
                if str(item.get("region", "")).upper() == request.customer_country
            ),
            None,
        )
        if direct:
            return direct
        raise InsufficientContextError(
            ["payment.surcharge_region"],
            provider="paypal",
            available_regions=[item.get("region") for item in surcharges],
        )

    def markets(self) -> list[MarketInfo]:
        result: list[MarketInfo] = []
        for country, market in sorted(self._countries.items()):
            index = self._index.get(country, {})
            result.append(
                MarketInfo(
                    provider=self.provider_id,
                    account_country=country,
                    market_code=str(market.get("paypal_market_code", country)),
                    locale=index.get("locale"),
                    status=str(market.get("derived_status", "unclassified")),
                    source_urls=[index["source_url"]] if index.get("source_url") else [],
                )
            )
        return result

    def capabilities(self, account_country: str) -> CapabilityInfo:
        country = account_country.upper()
        market = self._countries.get(country)
        if market is None:
            raise UnknownMarketError(self.provider_id, country)
        derived = market.get("derived") or {}
        types = [
            transaction_type.value
            for transaction_type, field in CATEGORY_FIELDS.items()
            if isinstance(derived.get(field), dict) and derived[field].get("percentage") is not None
        ]
        return CapabilityInfo(
            provider=self.provider_id,
            account_country=country,
            quotable=bool(types and derived.get("commercial_fixed_fees")),
            transaction_types=types,
            required_context=["payment.surcharge_region (international transactions only)"],
        )

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise QuoteUnavailableError(f"Invalid decimal value for {label}.", value=value) from exc
