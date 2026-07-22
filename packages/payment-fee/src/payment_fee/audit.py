from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from payment_fee.engine import PaymentFeeEngine
from payment_fee.errors import InsufficientTransactionContext, PaymentFeeError, QuoteNotAvailable
from payment_fee.models import Money, PayPalQuoteRequest, PayPalTransaction
from payment_fee.providers.paypal.models import PayPalCoreFees, PayPalCountryEntry, PayPalTransactionFeeRule
from payment_fee.providers.paypal.provider import (
    PAYPAL_API_FIELD_NAMES,
    PayPalProvider,
)
from payment_fee.providers.paypal.provider import (
    SUPPORTED_FEE_COMPONENT_TYPES as PAYPAL_SUPPORTED_COMPONENTS,
)
from payment_fee.providers.paypal.provider import (
    SUPPORTED_OPERATORS as PAYPAL_SUPPORTED_OPERATORS,
)
from payment_fee.providers.stripe.models import StripeCoreFees, StripeMarketEntry
from payment_fee.providers.stripe.provider import (
    STRIPE_API_FIELD_NAMES,
    StripeProvider,
)
from payment_fee.providers.stripe.provider import (
    SUPPORTED_COMPONENT_TYPES as STRIPE_SUPPORTED_COMPONENTS,
)
from payment_fee.providers.stripe.provider import (
    SUPPORTED_OPERATORS as STRIPE_SUPPORTED_OPERATORS,
)
from payment_fee.util import _as_list

PAYPAL_KNOWN_DIMENSIONS = {"amount", "applies_to_markets", "payment_methods"} | set(PAYPAL_API_FIELD_NAMES)

PAYPAL_KNOWN_OPERATORS = PAYPAL_SUPPORTED_OPERATORS

STRIPE_KNOWN_DIMENSIONS = set(STRIPE_API_FIELD_NAMES)

STRIPE_KNOWN_OPERATORS = STRIPE_SUPPORTED_OPERATORS


def _first_scalar(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


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

    def _route_paypal_dimension(dim: str, value: Any) -> None:
        """Place a PayPal condition value using the provider's API field name
        mapping, with all special-case transformations handled above.
        """
        path = PAYPAL_API_FIELD_NAMES.get(dim, f"transaction.context.{dim}")
        if path in {"amount.currency", "amount.value", "account_country", "customer_country"}:
            return
        if path.startswith("transaction.context."):
            tx_context[path.split(".")[-1]] = value
        elif path.startswith("transaction."):
            tx_kwargs[path.split(".")[-1]] = _first_scalar(value)
        else:
            tx_context[dim] = value

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
            scalar = _first_scalar(expected)
            if scalar is not None:
                transaction_region = str(scalar).lower()
        elif dimension == "customer_country":
            scalar = _first_scalar(expected)
            if scalar is not None:
                customer_country = str(scalar).upper()
        else:
            _route_paypal_dimension(dimension, expected)

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


def _audit_paypal(
    country: PayPalCountryEntry,
    result: ContractAuditResult,
) -> AuditCounters:
    counters = AuditCounters()
    provider = PayPalProvider(core=PayPalCoreFees(countries=[country]))
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
            provider._compile_single_rule_for_audit(rule, request)
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
    provider = StripeProvider(core=StripeCoreFees(markets=[market]))
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
            provider._compile_single_rule_for_audit(rule, currency)
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
