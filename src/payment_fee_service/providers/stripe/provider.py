from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from payment_fee_service.data.snapshots import ProviderSnapshot
from payment_fee_service.domain.errors import (
    AmbiguousRulesError,
    InsufficientContextError,
    QuoteUnavailableError,
    UnknownMarketError,
)
from payment_fee_service.domain.models import (
    CapabilityInfo,
    MarketInfo,
    QuoteRequest,
    StripeQuoteRequest,
)
from payment_fee_service.domain.rules import CompiledFeePlan, ExecutableFeeRule
from payment_fee_service.providers.stripe.matcher import (
    build_context,
    financial_signature,
    missing_dimensions,
    rule_matches_known_context,
    specificity,
)

NON_TRANSACTION_FIELDS = {"payout_type", "dispute_state"}


class StripeProvider:
    provider_id = "stripe"

    def __init__(self, snapshot: ProviderSnapshot) -> None:
        self.snapshot = snapshot
        self._markets = {
            str(item.get("account_country", "")).upper(): item
            for item in snapshot.core.get("markets", [])
        }
        self._index = {
            str(item.get("account_country", "")).upper(): item
            for item in snapshot.index.get("markets", [])
        }
        self._payment_methods = self._extract_payment_methods(snapshot.payment_methods or {})

    def compile_rules(self, request: QuoteRequest) -> CompiledFeePlan:
        if not isinstance(request, StripeQuoteRequest):
            raise TypeError("StripeProvider received a non-Stripe request")
        market = self._markets.get(request.account_country)
        if market is None:
            raise UnknownMarketError(self.provider_id, request.account_country)
        context = build_context(request)
        all_rules = [rule for rule in market.get("rules", []) if self._is_transaction_rule(rule)]

        relevant = [
            rule
            for rule in all_rules
            if self._method_scope_matches(rule, request.payment.method)
            and self._classification_usable(rule)
            and self._has_fee_value(rule)
        ]
        potentially_matching = [
            rule for rule in relevant if self._matches_non_missing(rule, context)
        ]
        missing = sorted(
            {
                dimension
                for rule in potentially_matching
                for dimension in missing_dimensions(rule, context)
            }
        )
        if missing:
            raise InsufficientContextError(
                [self._api_field_name(item) for item in missing],
                provider="stripe",
                candidate_rule_ids=[rule.get("rule_id") for rule in potentially_matching],
            )

        matched = [rule for rule in relevant if rule_matches_known_context(rule, context)]
        base = [
            rule
            for rule in matched
            if self._same_method(rule.get("payment_method"), request.payment.method)
        ]
        additives = [
            rule
            for rule in matched
            if rule.get("payment_method") is None and self._is_contextual_additive(rule)
        ]
        selected = self._resolve_base(base) + self._deduplicate(additives)
        if not selected:
            raise QuoteUnavailableError(
                "No classified Stripe transaction fee rule matched the supplied context.",
                provider="stripe",
                market=request.account_country,
                payment_method=request.payment.method,
            )

        index = self._index.get(request.account_country, {})
        rules = [self._to_executable(rule, request.amount.currency) for rule in selected]
        assumptions = [
            "Public standard pricing was used; negotiated or IC++ pricing is not represented.",
            "The published dataset does not encode provider settlement rounding, so "
            "standard currency rounding is used.",
        ]
        return CompiledFeePlan(
            provider=self.provider_id,
            market=request.account_country,
            currency=request.amount.currency,
            rules=rules,
            assumptions=assumptions,
            schema_version=self.snapshot.schema_version,
            content_sha256=index.get("content_sha256"),
            source_urls=list(index.get("source_urls") or []),
            source_updated_at=index.get("source_updated_at"),
            data_ref=self.snapshot.data_ref,
        )

    def _resolve_base(self, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rules:
            return []
        max_specificity = max(specificity(rule) for rule in rules)
        most_specific = [rule for rule in rules if specificity(rule) == max_specificity]
        signatures = {financial_signature(rule) for rule in most_specific}
        if len(signatures) > 1:
            raise AmbiguousRulesError([str(rule.get("rule_id")) for rule in most_specific])
        return [sorted(most_specific, key=lambda rule: str(rule.get("rule_id")))[0]]

    @staticmethod
    def _deduplicate(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for rule in sorted(rules, key=lambda item: str(item.get("rule_id"))):
            key = (
                financial_signature(rule),
                rule.get("name"),
                tuple(
                    sorted(
                        (c.get("dimension"), str(c.get("value")))
                        for c in rule.get("conditions", [])
                    )
                ),
            )
            unique.setdefault(key, rule)
        return list(unique.values())

    @staticmethod
    def _is_transaction_rule(rule: dict[str, Any]) -> bool:
        if any(rule.get(field) is not None for field in NON_TRANSACTION_FIELDS):
            return False
        return str(rule.get("unit", "per_transaction")) == "per_transaction"

    @staticmethod
    def _classification_usable(rule: dict[str, Any]) -> bool:
        status = str(rule.get("classification_status", "unclassified")).lower()
        return status not in {"", "unknown", "unclassified"}

    @staticmethod
    def _has_fee_value(rule: dict[str, Any]) -> bool:
        return any(
            rule.get(field) is not None
            for field in (
                "basis_points",
                "percentage",
                "fixed_amount",
                "minimum_amount",
                "maximum_amount",
            )
        )

    @staticmethod
    def _method_scope_matches(rule: dict[str, Any], requested_method: str) -> bool:
        method = rule.get("payment_method")
        if method is not None:
            return isinstance(method, str) and method.casefold() == requested_method.casefold()
        card_scoped = any(
            rule.get(field) is not None for field in ("card_origin", "card_region", "card_tier")
        )
        return not card_scoped or requested_method.casefold() == "card"

    @staticmethod
    def _same_method(value: Any, requested: str) -> bool:
        return isinstance(value, str) and value.casefold() == requested.casefold()

    @staticmethod
    def _matches_non_missing(rule: dict[str, Any], context: dict[str, Any]) -> bool:
        # Temporarily fill missing constrained values from the rule itself. This identifies rules
        # that could match once the caller supplies the missing context, without accepting them.
        probe = dict(context)
        for field, value in rule.items():
            if value is not None and probe.get(field) is None:
                probe[field] = value
        for condition in rule.get("conditions", []):
            dimension = condition.get("dimension")
            if dimension and probe.get(dimension) is None:
                probe[dimension] = condition.get("value")
        try:
            return rule_matches_known_context(rule, probe)
        except QuoteUnavailableError:
            raise

    @staticmethod
    def _is_contextual_additive(rule: dict[str, Any]) -> bool:
        return bool(
            rule.get("conditions")
            or rule.get("card_origin") is not None
            or rule.get("card_region") is not None
            or rule.get("card_tier") is not None
            or rule.get("currency_conversion_required") is not None
            or rule.get("customer_country") is not None
        )

    @staticmethod
    def _to_executable(rule: dict[str, Any], currency: str) -> ExecutableFeeRule:
        def decimal_or_none(field: str) -> Decimal | None:
            value = rule.get(field)
            if value is None:
                return None
            try:
                return Decimal(str(value))
            except InvalidOperation as exc:
                raise QuoteUnavailableError(
                    "A selected Stripe fee rule contains an invalid decimal.",
                    rule_id=rule.get("rule_id"),
                    field=field,
                    value=value,
                ) from exc

        behavior = str(rule.get("behavior", "additive"))
        if behavior not in {"additive", "base", "standard"}:
            raise QuoteUnavailableError(
                "Unsupported Stripe fee-rule behavior.",
                rule_id=rule.get("rule_id"),
                behavior=behavior,
            )
        fixed_currency = rule.get("fixed_currency")
        return ExecutableFeeRule(
            rule_id=str(rule["rule_id"]),
            label=str(rule.get("name") or rule.get("source_text") or "Stripe processing fee"),
            percentage=decimal_or_none("percentage"),
            basis_points=decimal_or_none("basis_points"),
            fixed_amount=decimal_or_none("fixed_amount"),
            fixed_currency=str(fixed_currency).upper() if fixed_currency else currency,
            minimum_amount=decimal_or_none("minimum_amount"),
            maximum_amount=decimal_or_none("maximum_amount"),
            behavior=behavior,
            classification_status=str(rule.get("classification_status", "classified")),
            confidence=float(rule["confidence"]) if rule.get("confidence") is not None else None,
            exactness=rule.get("exactness"),
            source_url=rule.get("source_url"),
        )

    def markets(self) -> list[MarketInfo]:
        return [
            MarketInfo(
                provider=self.provider_id,
                account_country=country,
                market_code=str(market.get("stripe_market_code", country.lower())),
                locale=market.get("locale"),
                status=str(market.get("derivation_status", "unclassified")),
                source_urls=list(self._index.get(country, {}).get("source_urls") or []),
            )
            for country, market in sorted(self._markets.items())
        ]

    def capabilities(self, account_country: str) -> CapabilityInfo:
        country = account_country.upper()
        market = self._markets.get(country)
        if market is None:
            raise UnknownMarketError(self.provider_id, country)
        methods = sorted(
            {
                str(rule["payment_method"])
                for rule in market.get("rules", [])
                if rule.get("payment_method")
                and self._classification_usable(rule)
                and self._has_fee_value(rule)
            }
        )
        required = sorted(
            {
                self._api_field_name(field)
                for rule in market.get("rules", [])
                for field in (
                    "card_origin",
                    "card_region",
                    "card_tier",
                    "channel",
                    "recurring",
                    "billing_type",
                    "currency_conversion_required",
                )
                if rule.get(field) is not None
            }
        )
        return CapabilityInfo(
            provider=self.provider_id,
            account_country=country,
            quotable=bool(methods),
            payment_methods=methods or sorted(self._payment_methods.get(country, set())),
            required_context=required,
        )

    @staticmethod
    def _api_field_name(dimension: str) -> str:
        mapping = {
            "payment_method": "payment.method",
            "card_origin": "payment.card.origin",
            "card_region": "payment.card.region",
            "card_tier": "payment.card.tier",
            "channel": "payment.channel",
            "recurring": "payment.recurring",
            "billing_type": "payment.billing_type",
            "currency_conversion_required": "payment.currency_conversion_required",
            "presentment_currency": "amount.currency",
            "settlement_currency": "settlement_currency",
            "customer_country": "customer_country",
        }
        return mapping.get(dimension, f"payment.context.{dimension}")

    @staticmethod
    def _extract_payment_methods(document: dict[str, Any]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        methods = document.get("payment_methods") or document.get("methods") or []
        for method in methods:
            method_id = method.get("method_id") or method.get("id")
            if not method_id:
                continue
            countries = (
                method.get("account_countries") or method.get("supported_account_countries") or []
            )
            for country in countries:
                result.setdefault(str(country).upper(), set()).add(str(method_id))
        return result
