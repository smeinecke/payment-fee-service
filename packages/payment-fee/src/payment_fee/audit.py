from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from payment_fee.engine import PaymentFeeEngine
from payment_fee.errors import InsufficientTransactionContext, PaymentFeeError, QuoteNotAvailable
from payment_fee.models import Money, PayPalQuoteRequest, PayPalTransaction, StripeQuoteRequest, StripeTransaction
from payment_fee.providers.paypal.models import PayPalCountryEntry, PayPalTransactionFeeRule
from payment_fee.providers.paypal.provider import (
    SUPPORTED_FEE_COMPONENT_TYPES as PAYPAL_SUPPORTED_COMPONENTS,
)
from payment_fee.providers.paypal.provider import (
    PayPalProvider,
)
from payment_fee.providers.stripe.models import StripeMarketEntry, StripeRule
from payment_fee.providers.stripe.provider import (
    SUPPORTED_COMPONENT_TYPES as STRIPE_SUPPORTED_COMPONENTS,
)
from payment_fee.providers.stripe.provider import (
    StripeProvider,
)

PAYPAL_KNOWN_DIMENSIONS = {
    "amount",
    "applies_to_markets",
    "payment_methods",
    "transaction_region",
    "payer_region",
    "surcharge_region",
    "customer_country",
    "payment_method",
    "merchant_approval_required",
    "pricing_plan",
    "withdrawal_method",
    "authorization_channel",
    "point_of_sale",
    "card_present",
    "transaction_purpose",
    "funding_source",
    "service",
    "recipient_location",
    "volume_status",
    "fee_currency",
}

PAYPAL_KNOWN_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}

STRIPE_KNOWN_DIMENSIONS = {
    "account_country",
    "customer_country",
    "amount_currency",
    "transaction_amount",
    "presentment_currency",
    "settlement_currency",
    "settlement_timing",
    "product_id",
    "variant_id",
    "payment_method",
    "payment_method_variant",
    "channel",
    "pricing_plan",
    "pricing_tier",
    "payer",
    "unit",
    "currency_conversion_required",
    "recurring",
    "billing_type",
    "transaction_region",
    "cross_border",
    "integration_type",
    "product_feature",
    "contract_length",
    "feature_enabled",
    "dispute_state",
    "fee_type",
    "success",
    "card_origin",
    "card_region",
    "card_type",
    "card_network",
    "card_tier",
    "card_entry_mode",
    "bank_account_validation",
    "bank_transfer_type",
    "transaction_type",
}

STRIPE_KNOWN_OPERATORS = {
    "eq",
    "==",
    "equals",
    "ne",
    "!=",
    "not_equals",
    "in",
    "not_in",
    "nin",
    "gt",
    "gte",
    "lt",
    "lte",
}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _first_scalar(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def _string_not_in(values: list[str]) -> str:
    base = "__audit_other__"
    candidate = base
    counter = 0
    while candidate in values:
        counter += 1
        candidate = f"{base}{counter}"
    return candidate


def _number_not_in(values: list[Any]) -> Decimal | int:
    try:
        nums = [Decimal(str(v)) for v in values]
    except Exception:
        nums = []
    if not nums:
        return Decimal("0")
    candidate = max(nums) + Decimal("1")
    while candidate in nums:
        candidate += Decimal("1")
    return candidate


def _actual_for_condition(value: Any, operator: str | None) -> Any:
    op = (operator or "eq").lower()
    values = _as_list(value)
    if op in {"in", "eq", "==", "equals"}:
        return _first_scalar(value)
    if op in {"not_in", "nin", "ne", "!=", "not_equals"}:
        if all(isinstance(v, str) for v in values):
            strs = [str(v) for v in values]
            return _string_not_in(strs)
        try:
            return _number_not_in(values)
        except Exception:
            return None
    return _first_scalar(value)


def _safe_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:
        return None


@dataclass
class ContractAuditResult:
    paypal_calculable_rules_total: int = 0
    paypal_calculable_rules_parsed: int = 0
    paypal_calculable_rules_skipped: int = 0
    paypal_context_required: int = 0
    stripe_calculable_rules_total: int = 0
    stripe_calculable_rules_parsed: int = 0
    stripe_calculable_rules_skipped: int = 0
    stripe_context_required: int = 0
    unknown_fields: int = 0
    unknown_condition_dimensions: int = 0
    unknown_condition_operators: int = 0
    unsupported_fee_components: int = 0
    unresolved_schedule_references: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass
class AuditCounters:
    total: int = 0
    parsed: int = 0
    skipped: int = 0
    context_required: int = 0


def _paypal_request_from_rule(
    rule: PayPalTransactionFeeRule,
    account_country: str,
    currency: str,
) -> PayPalQuoteRequest:
    amount_value = Decimal("100")
    amount_currency = currency
    customer_country = account_country
    transaction_region: str | None = None

    tx_kwargs: dict[str, Any] = {
        "product_id": rule.id,
        "variant_id": rule.variant_id,
    }
    tx_context: dict[str, Any] = {}

    for dimension, expected in rule.conditions.items():
        if dimension == "amount":
            if isinstance(expected, dict):
                if expected.get("currency"):
                    amount_currency = str(expected["currency"]).upper()
                op = str(expected.get("operator", "eq")).lower()
                val = _safe_decimal(expected.get("value"))
                if val is not None:
                    if op == "eq":
                        amount_value = val
                    elif op in {"gt", "ne"}:
                        amount_value = val + Decimal("1")
                    elif op == "gte":
                        amount_value = val
                    elif op == "lt":
                        amount_value = max(val - Decimal("1"), Decimal("0"))
                    elif op == "lte":
                        amount_value = val
        elif dimension == "applies_to_markets":
            markets = [str(v).upper() for v in _as_list(expected)]
            if "ALL_OTHER_MARKETS" in markets:
                continue
            if markets:
                selected = markets[0]
                if selected == account_country.upper():
                    customer_country = selected
                    transaction_region = "domestic"
                else:
                    customer_country = selected
                    transaction_region = "international"
        elif dimension == "payment_methods":
            methods = _as_list(expected)
            if methods:
                tx_kwargs["payment_method"] = str(methods[0]).lower()
        elif dimension == "transaction_region":
            transaction_region = str(expected).lower()
        elif dimension == "customer_country":
            customer_country = str(expected).upper()
        elif dimension in tx_kwargs or dimension in ("payer_region", "surcharge_region"):
            tx_kwargs[dimension] = expected
        else:
            tx_context[dimension] = expected

    if transaction_region is not None:
        tx_kwargs["transaction_region"] = transaction_region

    if tx_context:
        tx_kwargs["context"] = tx_context

    return PayPalQuoteRequest(
        provider="paypal",
        amount=Money(value=amount_value, currency=amount_currency),
        account_country=account_country,
        customer_country=customer_country,
        transaction=PayPalTransaction(**tx_kwargs),
    )


def _stripe_request_from_rule(
    rule: StripeRule,
    account_country: str,
    currency: str,
) -> StripeQuoteRequest:
    amount_value = Decimal("100")
    amount_currency = currency
    settlement_currency = currency
    customer_country = account_country

    # When a rule encodes a fixed amount in a specific currency, drive the
    # transaction currency from that currency unless an explicit condition says
    # otherwise.
    fixed_currency: str | None = None
    if rule.fixed_currency:
        fixed_currency = rule.fixed_currency.upper()
    else:
        for comp in rule.fee_components:
            if comp.type in {"fixed_amount", "fixed_surcharge", "minimum_fee", "maximum_fee"} and comp.currency:
                fixed_currency = comp.currency.upper()
                break
    if fixed_currency:
        amount_currency = fixed_currency
        settlement_currency = fixed_currency

    tx_kwargs: dict[str, Any] = {}
    tx_context: dict[str, Any] = {}
    card: dict[str, Any] = {}
    settlement: dict[str, Any] = {}
    bank: dict[str, Any] = {}

    transaction_fields = {
        "payment_method",
        "payment_method_variant",
        "channel",
        "pricing_plan",
        "pricing_tier",
        "payer",
        "unit",
        "currency_conversion_required",
        "recurring",
        "billing_type",
        "transaction_region",
        "cross_border",
        "integration_type",
        "product_feature",
        "contract_length",
        "feature_enabled",
        "dispute_state",
    }
    card_fields = {
        "card_origin": "origin",
        "card_region": "region",
        "card_tier": "tier",
        "card_type": "type",
        "card_network": "network",
        "card_entry_mode": "entry_mode",
    }

    def _set_tx(field: str, value: Any) -> None:
        tx_kwargs[field] = value

    for condition in list(rule.conditions):
        dim = condition.dimension
        val = condition.value
        op = condition.operator
        actual = _actual_for_condition(val, op)
        if dim == "transaction_amount":
            dec = _safe_decimal(val)
            if dec is not None:
                if op in {"gt", "gte"}:
                    amount_value = dec + Decimal("1")
                elif op in {"lt", "lte"}:
                    amount_value = max(dec - Decimal("1"), Decimal("0"))
                else:
                    amount_value = dec
        elif dim in {"amount_currency", "presentment_currency"}:
            amount_currency = str(actual).upper() if actual else currency
        elif dim == "settlement_currency":
            settlement_currency = str(actual).upper() if actual else currency
        elif dim == "account_country":
            account_country = str(actual).upper() if actual else account_country
        elif dim == "customer_country":
            customer_country = str(actual).upper() if actual else customer_country
        elif dim == "product_id":
            _set_tx("product_id", actual)
        elif dim == "variant_id":
            _set_tx("variant_id", actual)
        elif dim in transaction_fields:
            _set_tx(dim, actual)
        elif dim in card_fields:
            card[card_fields[dim]] = actual
        elif dim == "settlement_timing":
            settlement["timing"] = actual
        elif dim == "bank_account_validation":
            bank["validation"] = actual
        elif dim == "bank_transfer_type":
            bank["transfer_type"] = actual
        else:
            tx_context[dim] = actual

    if rule.transaction_amount_min is not None:
        dec = _safe_decimal(rule.transaction_amount_min)
        if dec is not None:
            amount_value = dec + Decimal("1")
    elif rule.transaction_amount_max is not None:
        dec = _safe_decimal(rule.transaction_amount_max)
        if dec is not None:
            amount_value = max(dec - Decimal("1"), Decimal("0"))

    if not tx_kwargs.get("product_id"):
        tx_kwargs["product_id"] = rule.product_id
    if not tx_kwargs.get("variant_id"):
        tx_kwargs["variant_id"] = rule.variant_id
    if tx_kwargs.get("unit") is None and rule.unit:
        tx_kwargs["unit"] = rule.unit

    if card:
        tx_kwargs["card"] = card
    if settlement:
        tx_kwargs["settlement"] = settlement
    if bank:
        tx_kwargs["bank"] = bank
    if tx_context:
        tx_kwargs["context"] = tx_context

    return StripeQuoteRequest(
        provider="stripe",
        amount=Money(value=amount_value, currency=amount_currency),
        account_country=account_country,
        customer_country=customer_country,
        settlement_currency=settlement_currency,
        transaction=StripeTransaction(**tx_kwargs),
    )


def _audit_paypal(
    country: PayPalCountryEntry,
    result: ContractAuditResult,
) -> AuditCounters:
    counters = AuditCounters()
    for rule in country.derived.transaction_fee_rules:
        if rule.calculation_status != "calculable":
            continue
        counters.total += 1
        if rule.conditions:
            counters.context_required += 1

        bad = False
        for comp in rule.fee_components:
            if comp.type not in PAYPAL_SUPPORTED_COMPONENTS:
                result.unsupported_fee_components += 1
                result.failures.append(
                    f"paypal:{country.country_code}:{rule.id}: unsupported fee component {comp.type!r}"
                )
                bad = True
        if bad:
            counters.skipped += 1
            continue

        for dimension in rule.conditions:
            if dimension not in PAYPAL_KNOWN_DIMENSIONS:
                result.unknown_condition_dimensions += 1
                result.failures.append(
                    f"paypal:{country.country_code}:{rule.id}: unknown condition dimension {dimension!r}"
                )
                bad = True
            elif dimension == "amount":
                expected = rule.conditions[dimension]
                if isinstance(expected, dict):
                    op = str(expected.get("operator", "eq")).lower()
                    if op not in PAYPAL_KNOWN_OPERATORS:
                        result.unknown_condition_operators += 1
                        result.failures.append(
                            f"paypal:{country.country_code}:{rule.id}: unknown amount operator {op!r}"
                        )
                        bad = True
        if bad:
            counters.skipped += 1
            continue

        schedule_attrs = {
            "fixed_fee_schedule": country.derived.fixed_fee_schedules,
            "international_surcharge_schedule": country.derived.international_surcharge_schedules,
            "maximum_fee_schedule": country.derived.maximum_fee_schedules,
        }
        for schedule_attr, schedules in schedule_attrs.items():
            schedule_id = getattr(rule, schedule_attr)
            if not schedule_id:
                continue
            if schedule_id not in schedules:
                result.unresolved_schedule_references += 1
                result.failures.append(
                    f"paypal:{country.country_code}:{rule.id}: unresolved {schedule_attr} {schedule_id!r}"
                )
                bad = True
        if bad:
            counters.skipped += 1
            continue

        currency = "USD"
        if rule.fixed_fee_schedule and country.derived.fixed_fee_schedules.get(rule.fixed_fee_schedule):
            entries = country.derived.fixed_fee_schedules[rule.fixed_fee_schedule].entries
            if entries:
                currency = next(iter(entries.keys()))
        elif rule.maximum_fee_schedule and country.derived.maximum_fee_schedules.get(rule.maximum_fee_schedule):
            entries = country.derived.maximum_fee_schedules[rule.maximum_fee_schedule].entries
            if entries:
                currency = next(iter(entries.keys()))
        elif rule.fee_components:
            for comp in rule.fee_components:
                if comp.currency:
                    currency = comp.currency
                    break

        request = _paypal_request_from_rule(rule, country.country_code, currency)
        try:
            from payment_fee.providers.paypal.models import PayPalCoreFees
            from payment_fee.providers.paypal.provider import PayPalProvider

            provider = PayPalProvider(core=PayPalCoreFees(countries=[country]))
            provider.compile_rules(request)
            counters.parsed += 1
        except InsufficientTransactionContext:
            counters.parsed += 1
        except QuoteNotAvailable as exc:
            if "No matching PayPal" in str(exc) and "schedule" in str(exc):
                result.unresolved_schedule_references += 1
                result.failures.append(f"paypal:{country.country_code}:{rule.id}: {exc}")
                counters.skipped += 1
            else:
                counters.parsed += 1
        except PaymentFeeError as exc:
            result.failures.append(f"paypal:{country.country_code}:{rule.id}: {exc}")
            counters.skipped += 1

    return counters


def _audit_stripe(market: StripeMarketEntry, result: ContractAuditResult) -> AuditCounters:
    counters = AuditCounters()
    for rule in market.rules:
        if rule.classification_status != "calculable_rule":
            continue

        unsupported_component = False
        for comp in rule.fee_components:
            if comp.type not in STRIPE_SUPPORTED_COMPONENTS:
                result.unsupported_fee_components += 1
                result.failures.append(
                    f"stripe:{market.account_country}:{rule.rule_id}: unsupported fee component {comp.type!r}"
                )
                unsupported_component = True
        if unsupported_component:
            counters.total += 1
            counters.skipped += 1
            continue

        bad = False
        for condition in rule.conditions:
            if condition.dimension not in STRIPE_KNOWN_DIMENSIONS:
                result.unknown_condition_dimensions += 1
                result.failures.append(
                    f"stripe:{market.account_country}:{rule.rule_id}: "
                    f"unknown condition dimension {condition.dimension!r}"
                )
                bad = True
            if condition.operator and str(condition.operator).lower() not in STRIPE_KNOWN_OPERATORS:
                result.unknown_condition_operators += 1
                result.failures.append(
                    f"stripe:{market.account_country}:{rule.rule_id}: unknown condition operator {condition.operator!r}"
                )
                bad = True
        if bad:
            counters.total += 1
            counters.skipped += 1
            continue

        counters.total += 1
        if rule.conditions:
            counters.context_required += 1

        currency = rule.fixed_currency
        if not currency:
            for comp in rule.fee_components:
                if comp.type in {"fixed_amount", "fixed_surcharge", "minimum_fee", "maximum_fee"} and comp.currency:
                    currency = comp.currency
                    break
        if not currency:
            currency = rule.presentment_currency or rule.settlement_currency or "USD"
        currency = currency.upper()

        try:
            from payment_fee.providers.stripe.provider import _executable_from_rule

            _executable_from_rule(rule, currency)
            counters.parsed += 1
        except PaymentFeeError as exc:
            result.failures.append(f"stripe:{market.account_country}:{rule.rule_id}: {exc}")
            counters.skipped += 1

    return counters


def audit_contract(engine: PaymentFeeEngine) -> ContractAuditResult:
    result = ContractAuditResult()
    for provider_id in engine.providers():
        provider = engine._registry.get(provider_id)
        if provider_id == "paypal" and isinstance(provider, PayPalProvider):
            for country in provider.core.countries:
                counters = _audit_paypal(country, result)
                result.paypal_calculable_rules_total += counters.total
                result.paypal_calculable_rules_parsed += counters.parsed
                result.paypal_calculable_rules_skipped += counters.skipped
                result.paypal_context_required += counters.context_required
        elif provider_id == "stripe" and isinstance(provider, StripeProvider):
            for market in provider.core.markets:
                counters = _audit_stripe(market, result)
                result.stripe_calculable_rules_total += counters.total
                result.stripe_calculable_rules_parsed += counters.parsed
                result.stripe_calculable_rules_skipped += counters.skipped
                result.stripe_context_required += counters.context_required
    return result
