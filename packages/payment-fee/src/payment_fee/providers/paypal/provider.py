from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, overload

from payment_fee.calculator import to_decimal
from payment_fee.data import load_json
from payment_fee.errors import (
    AmbiguousFeeRules,
    InsufficientTransactionContext,
    QuoteNotAvailable,
    UnknownMarket,
    UnsupportedFeeShape,
)
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, PayPalQuoteRequest, QuoteSchema
from payment_fee.providers.paypal.models import (
    PayPalCoreFees,
    PayPalCountryEntry,
    PayPalDerivedData,
    PayPalFixedFeeSchedule,
    PayPalIndex,
    PayPalIndexCountry,
    PayPalInternationalSurchargeSchedule,
    PayPalMaximumFeeSchedule,
    PayPalTransactionFeeRule,
)
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule

SUPPORTED_SCHEMA_VERSIONS = {1}

SUPPORTED_FEE_COMPONENT_TYPES = {
    "percentage",
    "fixed_amount",
    "fixed_fee_schedule",
    "international_surcharge_schedule",
    "maximum_fee_schedule",
}

_INDEX_COUNTRY_FIELDS = set(PayPalIndexCountry.model_fields.keys())
_CORE_TOP_FIELDS = set(PayPalCoreFees.model_fields.keys())
_COUNTRY_FIELDS = set(PayPalCountryEntry.model_fields.keys())


def _sanitize_index_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        return document
    allowed = _INDEX_COUNTRY_FIELDS | {"schema_version", "generated_at", "countries"}
    sanitized = {k: v for k, v in document.items() if k in allowed}
    sanitized["countries"] = [
        {k: v for k, v in country.items() if k in _INDEX_COUNTRY_FIELDS} for country in sanitized.get("countries", [])
    ]
    return sanitized


def _sanitize_core_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        return document
    sanitized = {k: v for k, v in document.items() if k in _CORE_TOP_FIELDS}
    sanitized["countries"] = [
        {k: v for k, v in country.items() if k in _COUNTRY_FIELDS} for country in sanitized.get("countries", [])
    ]
    return sanitized


@overload
def _resolve_schedule(
    derived: PayPalDerivedData,
    schedule_id: str,
    account_country: str,
    schedule_type: Literal["fixed_fee"],
    rule: PayPalTransactionFeeRule | None = None,
) -> PayPalFixedFeeSchedule | None: ...


@overload
def _resolve_schedule(
    derived: PayPalDerivedData,
    schedule_id: str,
    account_country: str,
    schedule_type: Literal["international_surcharge"],
    rule: PayPalTransactionFeeRule | None = None,
) -> PayPalInternationalSurchargeSchedule | None: ...


@overload
def _resolve_schedule(
    derived: PayPalDerivedData,
    schedule_id: str,
    account_country: str,
    schedule_type: Literal["maximum_fee"],
    rule: PayPalTransactionFeeRule | None = None,
) -> PayPalMaximumFeeSchedule | None: ...


def _resolve_schedule(
    derived: PayPalDerivedData,
    schedule_id: str,
    account_country: str,
    schedule_type: str,
    rule: PayPalTransactionFeeRule | None = None,
) -> PayPalFixedFeeSchedule | PayPalInternationalSurchargeSchedule | PayPalMaximumFeeSchedule | None:
    if not schedule_id:
        return None

    if schedule_type == "fixed_fee":
        schedules = derived.fixed_fee_schedules
    elif schedule_type == "international_surcharge":
        schedules = derived.international_surcharge_schedules
    elif schedule_type == "maximum_fee":
        schedules = derived.maximum_fee_schedules
    else:
        return None

    base_id = schedule_id.split("__", 1)[0]
    candidates = [
        key for key in schedules if key == base_id or (key.startswith(base_id) and "__" in key[len(base_id) :])
    ]

    best_key: str | None = None
    best_score: tuple[int, str, int] = (-1, "", 0)

    for key in candidates:
        score = _schedule_key_score(key, schedule_id, base_id, account_country, rule)
        if score is None:
            continue
        if best_key is None or score > best_score:
            best_key = key
            best_score = score

    if best_key:
        return schedules[best_key]
    return None


def _schedule_key_score(
    key: str,
    schedule_id: str,
    base_id: str,
    account_country: str,
    rule: PayPalTransactionFeeRule | None,
) -> tuple[int, str, int] | None:
    if key == schedule_id:
        if "__" in schedule_id:
            return (2, "", len(key))
        return (0, "", len(key))

    if "__" not in key:
        return None

    base_without_suffix, _, suffix = key.rpartition("__")
    if "=" not in suffix:
        return None

    dimension, _, value = suffix.partition("=")

    if dimension == "applies_to_markets":
        markets = [m.lower() for m in value.split("_")]
        if account_country.lower() not in markets and "all_other_markets" not in markets:
            return None
        if rule and "applies_to_markets" in rule.conditions:
            cond = rule.conditions["applies_to_markets"]
            if isinstance(cond, list):
                cond_values = [str(v).lower() for v in cond]
                if "all_other_markets" not in cond_values:
                    return (1, "applies_to_markets", len(key))
        return None

    if dimension == "pricing_plan":
        if rule and rule.conditions.get("pricing_plan") == value:
            specificity = 0
            if base_without_suffix.rstrip("_").endswith(value) or ("_" + value) in base_without_suffix:
                specificity = 1
            return (1, f"pricing_plan_{specificity}", len(key))
        return None

    return None


def _fixed_fee_amount(
    schedule: PayPalFixedFeeSchedule | None,
    currency: str,
    rule_id: str,
    schedule_name: str,
) -> Decimal:
    if schedule is None:
        raise QuoteNotAvailable(
            "The selected PayPal fee category has no fixed-fee schedule.",
            rule_id=rule_id,
            schedule=schedule_name,
        )
    raw = schedule.entries.get(currency)
    if raw is None:
        raise QuoteNotAvailable(
            "No PayPal fixed fee is published for the transaction currency.",
            rule_id=rule_id,
            currency=currency,
            schedule=schedule_name,
        )
    return to_decimal(raw, "fixed fee")


def _international_surcharge_rate(
    schedule: PayPalInternationalSurchargeSchedule,
    payer_region: str,
    rule_id: str,
    schedule_name: str,
) -> Decimal | None:
    if schedule is None or payer_region is None:
        return None
    for entry in schedule.entries:
        if entry.payer_region.upper() == payer_region.upper():
            if entry.percentage_points is None:
                return None
            return to_decimal(entry.percentage_points, "surcharge percentage")
    return None


def _maximum_fee_amount(
    schedule: PayPalMaximumFeeSchedule | None,
    currency: str,
    rule_id: str,
    schedule_name: str,
) -> Decimal:
    if schedule is None:
        raise QuoteNotAvailable(
            "The selected PayPal fee category has no maximum-fee schedule.",
            rule_id=rule_id,
            schedule=schedule_name,
        )
    raw = schedule.entries.get(currency)
    if raw is None:
        raise QuoteNotAvailable(
            "No PayPal maximum fee is published for the transaction currency.",
            rule_id=rule_id,
            currency=currency,
            schedule=schedule_name,
        )
    return to_decimal(raw, "maximum fee")


def _build_paypal_context(request: PayPalQuoteRequest) -> dict[str, Any]:
    transaction = request.transaction
    context: dict[str, Any] = {
        "account_country": request.account_country,
        "customer_country": request.customer_country,
        "amount_currency": request.amount.currency,
        "transaction_amount": request.amount.value,
        "product_id": transaction.product_id,
        "variant_id": transaction.variant_id,
        "payment_method": transaction.payment_method,
        "payer_region": transaction.payer_region,
        "surcharge_region": transaction.surcharge_region,
        "merchant_approval_required": transaction.merchant_approval_required,
        "pricing_plan": transaction.pricing_plan,
        "withdrawal_method": transaction.withdrawal_method,
        "authorization_channel": transaction.authorization_channel,
        "point_of_sale": transaction.point_of_sale,
        "card_present": transaction.card_present,
        "transaction_purpose": transaction.transaction_purpose,
        "funding_source": transaction.funding_source,
        "service": transaction.service,
        "recipient_location": transaction.recipient_location,
        "volume_status": transaction.volume_status,
        "fee_currency": transaction.fee_currency or request.amount.currency,
    }

    if transaction.transaction_region is not None:
        context["transaction_region"] = transaction.transaction_region.lower()
    elif request.customer_country is not None:
        context["transaction_region"] = (
            "domestic" if request.customer_country == request.account_country else "international"
        )
    else:
        context["transaction_region"] = "domestic"

    for key, value in transaction.context.items():
        if key in context:
            if value != context[key]:
                raise QuoteNotAvailable(
                    "Contradictory duplicate value in transaction context.",
                    field=key,
                    typed_value=context[key],
                    context_value=value,
                )
        else:
            context[key] = value

    return context


def _condition_matches(rule: PayPalTransactionFeeRule, context: dict[str, Any]) -> bool:
    return all(_value_matches(dimension, expected, context) for dimension, expected in rule.conditions.items())


def _value_matches(dimension: str, expected: Any, context: dict[str, Any]) -> bool:
    if dimension == "amount":
        return _amount_condition_matches(expected, context)

    if dimension == "applies_to_markets":
        return _applies_to_markets_matches(expected, context)

    if dimension == "payment_methods":
        return _payment_methods_matches(expected, context)

    actual = context.get(dimension)
    if actual is None and expected is not None:
        return False

    if isinstance(expected, bool):
        return actual is expected

    if isinstance(expected, list):
        return str(actual).casefold() in [str(v).casefold() for v in expected]

    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except Exception:
            return False

    return str(actual).casefold() == str(expected).casefold()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _applies_to_markets_matches(expected: Any, context: dict[str, Any]) -> bool:
    transaction_region = str(context.get("transaction_region", "")).lower()
    if transaction_region == "international":
        target = context.get("customer_country")
    else:
        target = context.get("account_country")
    values = _as_list(expected)
    if "all_other_markets" in [str(v).lower() for v in values]:
        return True
    if target is None:
        return False
    return target.upper() in [str(v).upper() for v in values]


def _payment_methods_matches(expected: Any, context: dict[str, Any]) -> bool:
    actual = context.get("payment_method")
    if actual is None:
        return False
    values = _as_list(expected)
    return str(actual).casefold() in [str(v).casefold() for v in values]


def _amount_condition_matches(expected: Any, context: dict[str, Any]) -> bool:
    if not isinstance(expected, dict):
        return False
    condition_currency = expected.get("currency")
    if condition_currency and condition_currency.upper() != context.get("amount_currency", "").upper():
        return False
    operator = str(expected.get("operator", "eq")).lower()
    try:
        right = Decimal(str(expected.get("value")))
        left = context.get("transaction_amount")
        if left is None:
            return False
        left = Decimal(str(left))
    except Exception:
        return False
    return {
        "eq": left == right,
        "ne": left != right,
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }.get(operator, False)


def _missing_dimensions(rule: PayPalTransactionFeeRule, context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for dimension in rule.conditions:
        if dimension == "amount":
            continue
        if dimension == "applies_to_markets":
            expected = rule.conditions[dimension]
            values = _as_list(expected)
            if "all_other_markets" in [str(v).lower() for v in values]:
                continue
            transaction_region = str(context.get("transaction_region", "")).lower()
            if transaction_region == "international" and context.get("customer_country") is None:
                missing.append("customer_country")
            continue
        if dimension == "payment_methods":
            if context.get("payment_method") is None:
                missing.append("transaction.payment_method")
            continue
        if context.get(dimension) is None:
            missing.append(_api_field_name(dimension))
    return missing


def _api_field_name(dimension: str) -> str:
    mapping = {
        "product_id": "transaction.product_id",
        "variant_id": "transaction.variant_id",
        "payment_method": "transaction.payment_method",
        "transaction_region": "transaction.transaction_region",
        "payer_region": "transaction.payer_region",
        "surcharge_region": "transaction.surcharge_region",
        "merchant_approval_required": "transaction.merchant_approval_required",
        "pricing_plan": "transaction.pricing_plan",
        "withdrawal_method": "transaction.withdrawal_method",
        "authorization_channel": "transaction.authorization_channel",
        "point_of_sale": "transaction.point_of_sale",
        "card_present": "transaction.card_present",
        "transaction_purpose": "transaction.transaction_purpose",
        "funding_source": "transaction.funding_source",
        "service": "transaction.service",
        "recipient_location": "transaction.recipient_location",
        "volume_status": "transaction.volume_status",
        "fee_currency": "transaction.fee_currency",
    }
    return mapping.get(dimension, f"transaction.context.{dimension}")


def _specificity(rule: PayPalTransactionFeeRule) -> int:
    score = 0
    if rule.variant_id:
        score += 1
    for dimension, expected in rule.conditions.items():
        if dimension == "applies_to_markets":
            values = _as_list(expected)
            if any(str(v).lower() != "all_other_markets" for v in values):
                score += 2
            else:
                score += 1
        elif dimension == "pricing_plan":
            score += 2
        else:
            score += 1
    return score


def _rule_percentage(rule: PayPalTransactionFeeRule) -> str | None:
    if rule.percentage:
        return rule.percentage
    for comp in rule.fee_components:
        if comp.type == "percentage" and comp.value:
            return comp.value
    return None


def _component_schedule_id(rule: PayPalTransactionFeeRule, schedule_type: str) -> str | None:
    for comp in rule.fee_components:
        if comp.type == schedule_type:
            return comp.schedule_id
    return None


def _rule_signature(
    rule: PayPalTransactionFeeRule,
    derived: PayPalDerivedData,
    account_country: str,
    currency: str,
    payer_region: str | None,
    context: dict[str, Any],
) -> tuple[Any, ...]:
    percentage = _rule_percentage(rule)

    fixed_amount: Any = None
    max_amount: Any = None
    surcharge_rate: Any = None

    fixed_schedule_name = rule.fixed_fee_schedule or _component_schedule_id(rule, "fixed_fee_schedule")
    if fixed_schedule_name:
        fixed_schedule = _resolve_schedule(derived, fixed_schedule_name, account_country, "fixed_fee", rule)
        if fixed_schedule:
            fixed_amount = fixed_schedule.entries.get(currency)

    for comp in rule.fee_components:
        if comp.type == "fixed_amount" and comp.amount is not None and fixed_amount is None:
            fixed_amount = comp.amount

    max_schedule_name = rule.maximum_fee_schedule or _component_schedule_id(rule, "maximum_fee_schedule")
    if max_schedule_name:
        max_schedule = _resolve_schedule(derived, max_schedule_name, account_country, "maximum_fee", rule)
        if max_schedule:
            max_amount = max_schedule.entries.get(currency)

    surcharge_schedule_name = rule.international_surcharge_schedule or _component_schedule_id(
        rule, "international_surcharge_schedule"
    )
    if surcharge_schedule_name and payer_region:
        surcharge_schedule = _resolve_schedule(
            derived, surcharge_schedule_name, account_country, "international_surcharge", rule
        )
        if surcharge_schedule:
            surcharge_rate = _international_surcharge_rate(
                surcharge_schedule, payer_region, rule.id, surcharge_schedule_name
            )

    return (percentage, fixed_amount, max_amount, surcharge_rate)


def _compile_rule(
    rule: PayPalTransactionFeeRule,
    derived: PayPalDerivedData,
    request: PayPalQuoteRequest,
    context: dict[str, Any],
    source_url: str | None,
) -> list[ExecutableFeeRule]:
    currency = request.amount.currency
    account_country = request.account_country
    payer_region = context.get("payer_region") or context.get("surcharge_region")

    percentage_raw = _rule_percentage(rule)
    percentage = to_decimal(percentage_raw, "percentage") if percentage_raw else None

    fixed_amount: Decimal | None = None
    fixed_currency: str | None = None

    for comp in rule.fee_components:
        if comp.type == "fixed_amount" and comp.amount is not None:
            fa = to_decimal(comp.amount, "fixed amount")
            fc = comp.currency or currency
            if fixed_amount is None:
                fixed_amount = Decimal("0")
                fixed_currency = fc
            fixed_amount += fa

    fixed_schedule_name = rule.fixed_fee_schedule or _component_schedule_id(rule, "fixed_fee_schedule")
    if fixed_schedule_name:
        fixed_schedule = _resolve_schedule(derived, fixed_schedule_name, account_country, "fixed_fee", rule)
        schedule_fixed = _fixed_fee_amount(fixed_schedule, currency, rule.id, fixed_schedule_name)
        if fixed_amount is None:
            fixed_amount = Decimal("0")
            fixed_currency = currency
        fixed_amount += schedule_fixed

    maximum_amount: Decimal | None = None
    max_schedule_name = rule.maximum_fee_schedule or _component_schedule_id(rule, "maximum_fee_schedule")
    if max_schedule_name:
        max_schedule = _resolve_schedule(derived, max_schedule_name, account_country, "maximum_fee", rule)
        maximum_amount = _maximum_fee_amount(max_schedule, currency, rule.id, max_schedule_name)

    surcharge_rate: Decimal | None = None
    surcharge_schedule_name = rule.international_surcharge_schedule or _component_schedule_id(
        rule, "international_surcharge_schedule"
    )
    if surcharge_schedule_name:
        surcharge_schedule = _resolve_schedule(
            derived, surcharge_schedule_name, account_country, "international_surcharge", rule
        )
        if surcharge_schedule is None:
            raise QuoteNotAvailable(
                "The selected PayPal fee category has no international surcharge schedule.",
                rule_id=rule.id,
                schedule=surcharge_schedule_name,
            )
        if payer_region:
            surcharge_rate = _international_surcharge_rate(
                surcharge_schedule, payer_region, rule.id, surcharge_schedule_name
            )

    executable_rules: list[ExecutableFeeRule] = []

    if percentage is not None or fixed_amount is not None or maximum_amount is not None:
        executable_rules.append(
            ExecutableFeeRule(
                rule_id=f"paypal:{account_country}:{rule.id}:{rule.variant_id or 'default'}:base",
                label=rule.label or rule.id,
                component_type="processing",
                behavior="base",
                percentage=percentage,
                fixed_amount=fixed_amount,
                fixed_currency=fixed_currency or currency,
                maximum_amount=maximum_amount,
                currency=currency,
                classification_status=rule.calculation_status,
                exactness="exact",
                confidence=1.0,
                source_url=source_url,
                metadata={
                    "product_id": rule.id,
                    "variant_id": rule.variant_id,
                },
            )
        )

    if surcharge_rate is not None:
        executable_rules.append(
            ExecutableFeeRule(
                rule_id=f"paypal:{account_country}:{rule.id}:{rule.variant_id or 'default'}:surcharge:{payer_region}",
                label=f"International surcharge ({payer_region})",
                component_type="surcharge",
                behavior="additive",
                percentage=surcharge_rate,
                currency=currency,
                classification_status=rule.calculation_status,
                exactness="exact",
                confidence=1.0,
                source_url=source_url,
                metadata={
                    "product_id": rule.id,
                    "variant_id": rule.variant_id,
                    "payer_region": payer_region,
                },
            )
        )

    if not executable_rules:
        raise QuoteNotAvailable(
            "The selected PayPal fee rule has no calculable components.",
            rule_id=rule.id,
        )

    return executable_rules


class PayPalProvider:
    provider_id = "paypal"

    def __init__(
        self,
        core: PayPalCoreFees,
        index: PayPalIndex | None = None,
        data_ref: str | None = None,
    ) -> None:
        self.core = core
        self.index = index
        self.data_ref = data_ref
        self._countries = {c.country_code.upper(): c for c in core.countries}
        self._index_map: dict[str, PayPalIndexCountry] = {}
        if index:
            self._index_map = {ic.country_code.upper(): ic for ic in index.countries}

    @classmethod
    def from_paths(
        cls,
        path: str,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> PayPalProvider:
        core_path = load_json(f"{path}/json/core-fees.json")
        index_path = load_json(f"{path}/json/index.json")
        core_document = _sanitize_core_document(core_path)
        index_document = _sanitize_index_document(index_path)
        if validate_schema:
            from payment_fee.data import validate_json_schema

            validate_json_schema(core_document, f"{path}/schemas/core-fees-v1.schema.json", "paypal-core")
            validate_json_schema(index_document, f"{path}/schemas/index-v1.schema.json", "paypal-index")
        core = PayPalCoreFees.model_validate(core_document)
        index = PayPalIndex.model_validate(index_document)
        if core.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedFeeShape(
                f"Unsupported PayPal schema version: {core.schema_version}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return cls(core=core, index=index, data_ref=data_ref)

    @classmethod
    def from_documents(
        cls,
        core: dict[str, Any],
        index: dict[str, Any] | None = None,
        data_ref: str | None = None,
    ) -> PayPalProvider:
        core_model = PayPalCoreFees.model_validate(_sanitize_core_document(core))
        index_model = PayPalIndex.model_validate(_sanitize_index_document(index)) if index else None
        if core_model.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedFeeShape(
                f"Unsupported PayPal schema version: {core_model.schema_version}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return cls(core=core_model, index=index_model, data_ref=data_ref)

    def _country(self, code: str) -> PayPalCountryEntry:
        code = code.upper()
        country = self._countries.get(code)
        if country is None:
            raise UnknownMarket(self.provider_id, code)
        return country

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan:
        if not isinstance(request, PayPalQuoteRequest):
            raise TypeError(f"Expected PayPalQuoteRequest, got {type(request).__name__}")
        country = self._country(request.account_country)
        derived = country.derived
        context = _build_paypal_context(request)

        product_id = context.get("product_id")
        if not product_id:
            available = sorted({r.id for r in derived.transaction_fee_rules})
            raise InsufficientTransactionContext(
                ["transaction.product_id"],
                provider=self.provider_id,
                market=request.account_country,
                available_product_ids=available,
            )

        product_id = str(product_id).lower()
        product_rules = [r for r in derived.transaction_fee_rules if r.id.lower() == product_id]

        variant_id = context.get("variant_id")
        if variant_id:
            variant_id = str(variant_id).lower()
            product_rules = [r for r in product_rules if (r.variant_id or "").lower() == variant_id]

        if not product_rules:
            raise QuoteNotAvailable(
                "The requested PayPal product/variant is not classified for this market.",
                market=request.account_country,
                product_id=product_id,
                variant_id=variant_id,
            )

        missing: set[str] = set()
        matching: list[PayPalTransactionFeeRule] = []
        for rule in product_rules:
            rule_missing = _missing_dimensions(rule, context)
            if rule_missing:
                missing.update(rule_missing)
                continue
            if _condition_matches(rule, context):
                matching.append(rule)

        if not matching and missing:
            raise InsufficientTransactionContext(
                sorted(missing),
                provider=self.provider_id,
                market=request.account_country,
            )

        if not matching:
            raise QuoteNotAvailable(
                "No PayPal fee rule matched the supplied context.",
                market=request.account_country,
                product_id=product_id,
                variant_id=variant_id,
            )

        for rule in matching:
            if rule.calculation_status != "calculable":
                raise QuoteNotAvailable(
                    "A selected PayPal rule is not calculable.",
                    rule_id=rule.id,
                    status=rule.calculation_status,
                )

        max_specificity = max(_specificity(rule) for rule in matching)
        most_specific = [r for r in matching if _specificity(r) == max_specificity]

        payer_region = context.get("payer_region") or context.get("surcharge_region")
        if len(most_specific) > 1:
            signatures = {
                _rule_signature(r, derived, request.account_country, request.amount.currency, payer_region, context)
                for r in most_specific
            }
            if len(signatures) > 1:
                raise AmbiguousFeeRules(
                    [r.id for r in most_specific],
                    provider=self.provider_id,
                    market=request.account_country,
                )

        selected = sorted(most_specific, key=lambda r: (r.id, r.variant_id or ""))[0]

        surcharge_schedule_name = (
            selected.international_surcharge_schedule
            or _component_schedule_id(selected, "international_surcharge_schedule")
            or ""
        )
        if surcharge_schedule_name and payer_region is None and context.get("transaction_region") != "domestic":
            surcharge_schedule = _resolve_schedule(
                derived, surcharge_schedule_name, request.account_country, "international_surcharge", selected
            )
            if surcharge_schedule is None:
                raise QuoteNotAvailable(
                    "The selected PayPal fee category has no international surcharge schedule.",
                    rule_id=selected.id,
                    schedule=surcharge_schedule_name,
                )
            available_regions = [e.payer_region for e in surcharge_schedule.entries]
            raise InsufficientTransactionContext(
                ["transaction.payer_region", "transaction.surcharge_region"],
                provider=self.provider_id,
                market=request.account_country,
                available_surcharge_regions=sorted(set(available_regions)),
            )

        source_url: str | None = None
        if selected.source:
            source_url = selected.source.canonical_url or selected.source.requested_url
        if not source_url:
            index_entry = self._index_map.get(request.account_country.upper())
            source_url = index_entry.source_url if index_entry else None

        executable_rules = _compile_rule(selected, derived, request, context, source_url)

        assumptions = [
            "Public standard pricing was used; negotiated merchant pricing is not represented.",
            "The published dataset does not encode provider settlement rounding, so "
            "standard currency rounding is used.",
        ]

        index_entry = self._index_map.get(request.account_country.upper())
        return CompiledFeePlan(
            provider=self.provider_id,
            market=request.account_country,
            currency=request.amount.currency,
            rules=executable_rules,
            assumptions=assumptions,
            schema_version=self.core.schema_version,
            content_sha256=index_entry.content_sha256 if index_entry else None,
            source_urls=[source_url] if source_url else [],
            source_updated_at=index_entry.source_updated_at if index_entry else None,
            data_ref=self.data_ref,
            product_id=selected.id,
            variant_id=selected.variant_id,
        )

    def markets(self) -> list[MarketInfo]:
        result: list[MarketInfo] = []
        for code, country in sorted(self._countries.items()):
            index = self._index_map.get(code)
            market_code = country.paypal_market_code or code
            locale = index.locale if index else None
            status = country.derived_status or (index.derived_status if index else "unclassified")
            source_urls = [index.source_url] if index and index.source_url else []
            result.append(
                MarketInfo(
                    provider=self.provider_id,
                    account_country=code,
                    market_code=market_code,
                    locale=locale,
                    status=status,
                    source_urls=source_urls,
                )
            )
        return result

    def capabilities(self, account_country: str) -> CapabilityInfo:
        country = self._country(account_country)
        derived = country.derived

        products: set[str] = set()
        variants: set[str] = set()
        payment_methods: set[str] = set()
        fee_shapes: set[str] = set()
        currencies: set[str] = set()
        dimensions: set[str] = set()
        allowed: dict[str, set[Any]] = {}

        calculable: dict[str, set[str]] = {}
        included: dict[str, set[str]] = {}
        custom_pricing: dict[str, set[str]] = {}
        unsupported: dict[str, set[str]] = {}
        non_calculable: dict[str, set[str]] = {}

        for rule in derived.transaction_fee_rules:
            product_id = rule.id
            variant_id = rule.variant_id or "default"
            products.add(product_id)
            if rule.variant_id:
                variants.add(rule.variant_id)

            supported = all(comp.type in SUPPORTED_FEE_COMPONENT_TYPES for comp in rule.fee_components)

            target = non_calculable
            if rule.calculation_status == "calculable" and supported:
                target = calculable
            elif rule.calculation_status == "included":
                target = included
            elif rule.calculation_status == "custom_pricing":
                target = custom_pricing
            elif rule.calculation_status == "unsupported" or not supported:
                target = unsupported
            elif rule.calculation_status == "non_calculable":
                target = non_calculable

            target.setdefault(product_id, set()).add(variant_id)

            for comp in rule.fee_components:
                if comp.type in SUPPORTED_FEE_COMPONENT_TYPES:
                    fee_shapes.add(comp.type)
                if comp.type == "fixed_amount" and comp.currency:
                    currencies.add(comp.currency.upper())

            for dim, value in rule.conditions.items():
                dimensions.add(dim)
                if dim not in allowed:
                    allowed[dim] = set()
                if isinstance(value, list):
                    for item in value:
                        allowed[dim].add(item if isinstance(item, bool) else str(item))
                elif isinstance(value, bool):
                    allowed[dim].add(value)
                elif (isinstance(value, (int, float)) and not isinstance(value, bool)) or isinstance(value, dict):
                    allowed[dim].add(str(value))
                else:
                    allowed[dim].add(str(value))

            if "payment_methods" in rule.conditions:
                payment_methods.update(str(v) for v in _as_list(rule.conditions["payment_methods"]))

        for schedule in derived.fixed_fee_schedules.values():
            currencies.update(schedule.entries.keys())

        for schedule in derived.maximum_fee_schedules.values():
            currencies.update(schedule.entries.keys())

        index_entry = self._index_map.get(account_country.upper())
        return CapabilityInfo(
            provider=self.provider_id,
            account_country=account_country.upper(),
            quotable=bool(derived.transaction_fee_rules),
            product_ids=sorted(products),
            variants=sorted(variants),
            payment_methods=sorted(payment_methods),
            supported_fee_shapes=sorted(fee_shapes),
            supported_currencies=sorted(currencies),
            condition_dimensions=sorted(dimensions),
            allowed_values={k: sorted(v) for k, v in allowed.items()},
            required_context=sorted(_required_context(derived)),
            calculable_products={k: sorted(v) for k, v in calculable.items()},
            included_products={k: sorted(v) for k, v in included.items()},
            custom_pricing_products={k: sorted(v) for k, v in custom_pricing.items()},
            unsupported_products={k: sorted(v) for k, v in unsupported.items()},
            non_calculable_products={k: sorted(v) for k, v in non_calculable.items()},
            dataset_status=country.derived_status or (index_entry.derived_status if index_entry else "unknown"),
            source_revision=index_entry.content_sha256 if index_entry else None,
        )

    def quote_schema(self, account_country: str) -> QuoteSchema:
        cap = self.capabilities(account_country)
        return QuoteSchema(
            provider=self.provider_id,
            account_country=account_country.upper(),
            request_schema=_paypal_request_schema(cap),
            response_schema={},
        )

    def data_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "schema_version": self.core.schema_version,
            "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
            "market_count": len(self._countries),
            "generated_at": self.core.generated_at,
            "data_ref": self.data_ref,
        }


def _required_context(derived: PayPalDerivedData) -> set[str]:
    required: set[str] = set()
    for rule in derived.transaction_fee_rules:
        for dim in rule.conditions:
            if dim == "amount":
                continue
            if dim == "applies_to_markets":
                continue
            if dim == "payment_methods":
                required.add("transaction.payment_method")
            else:
                required.add(_api_field_name(dim))
    return required


def _paypal_request_schema(cap: CapabilityInfo) -> dict[str, Any]:
    transaction_properties: dict[str, Any] = {
        "product_id": {"enum": cap.product_ids} if cap.product_ids else {"type": "string"},
        "variant_id": {"enum": cap.variants} if cap.variants else {"type": "string"},
        "payment_method": {"enum": cap.payment_methods} if cap.payment_methods else {"type": "string"},
        "transaction_region": {"enum": ["domestic", "international"]},
        "payer_region": {"type": "string"},
        "surcharge_region": {"type": "string"},
        "merchant_approval_required": {"type": "boolean"},
        "pricing_plan": {"type": "string"},
        "withdrawal_method": {"type": "string"},
        "authorization_channel": {"type": "string"},
        "point_of_sale": {"type": "boolean"},
        "card_present": {"type": "boolean"},
        "transaction_purpose": {"type": "string"},
        "funding_source": {"type": "string"},
        "service": {"type": "string"},
        "recipient_location": {"type": "string"},
        "volume_status": {"type": "string"},
        "fee_currency": {"type": "string"},
        "context": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "boolean", "null", "array"]},
        },
    }

    dimension_to_property = {
        "payment_methods": "payment_method",
        "transaction_region": "transaction_region",
        "payer_region": "payer_region",
        "surcharge_region": "surcharge_region",
        "merchant_approval_required": "merchant_approval_required",
        "pricing_plan": "pricing_plan",
        "withdrawal_method": "withdrawal_method",
        "authorization_channel": "authorization_channel",
        "point_of_sale": "point_of_sale",
        "card_present": "card_present",
        "transaction_purpose": "transaction_purpose",
        "funding_source": "funding_source",
        "service": "service",
        "recipient_location": "recipient_location",
        "volume_status": "volume_status",
        "fee_currency": "fee_currency",
    }

    for dim in cap.condition_dimensions:
        if dim in dimension_to_property:
            prop = dimension_to_property[dim]
            if prop in transaction_properties and cap.allowed_values.get(dim):
                values = cap.allowed_values[dim]
                if all(isinstance(v, bool) for v in values):
                    transaction_properties[prop] = {"type": "boolean"}
                else:
                    transaction_properties[prop] = {"enum": values}

    return {
        "type": "object",
        "properties": {
            "provider": {"enum": ["paypal"]},
            "amount": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "currency": {"type": "string", "minLength": 3, "maxLength": 3},
                },
                "required": ["value", "currency"],
            },
            "account_country": {"type": "string", "minLength": 2, "maxLength": 2},
            "customer_country": {"type": "string", "minLength": 2, "maxLength": 2},
            "settlement_currency": {"type": "string", "minLength": 3, "maxLength": 3},
            "transaction": {
                "type": "object",
                "properties": transaction_properties,
                "additionalProperties": False,
            },
        },
        "required": ["provider", "amount", "account_country", "transaction"],
    }
