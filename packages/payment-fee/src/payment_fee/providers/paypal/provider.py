from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from payment_fee.calculator import to_decimal
from payment_fee.data import load_json
from payment_fee.errors import (
    DatasetValidationError,
    InsufficientTransactionContext,
    QuoteNotAvailable,
    UnknownMarket,
)
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, PayPalQuoteRequest, QuoteSchema
from payment_fee.providers.base import (
    SUPPORTED_SCHEMA_VERSIONS,
    CapabilityAccumulator,
    NormalizedCondition,
    _api_field_name_lookup,
    _check_schema_version,
    _merge_context_overrides,
    compile_generic,
)
from payment_fee.providers.paypal.adapter import (
    adapt_paypal_core_document,
    adapt_paypal_index_document,
)
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
from payment_fee.util import _as_list

SUPPORTED_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}

SUPPORTED_FEE_COMPONENT_TYPES = {
    "percentage",
    "fixed_amount",
    "fixed_fee_schedule",
    "international_surcharge_schedule",
    "maximum_fee_schedule",
}


@dataclass(frozen=True)
class _ScheduleIdentity:
    raw_id: str
    stem: str
    applies_to_markets: frozenset[str] | None
    pricing_plan: str | None

    @classmethod
    def parse(cls, raw_id: str) -> _ScheduleIdentity:
        parts = raw_id.split("__")
        stem = parts[0]
        markets: frozenset[str] | None = None
        pricing_plan: str | None = None
        for part in parts[1:]:
            if "=" not in part:
                raise DatasetValidationError(
                    f"Malformed PayPal schedule selector {part!r} in {raw_id!r}.",
                    schedule_id=raw_id,
                    selector=part,
                )
            dimension, _, value = part.partition("=")
            if dimension == "applies_to_markets":
                markets = frozenset(value.split("_"))
            elif dimension == "pricing_plan":
                pricing_plan = value
            else:
                raise DatasetValidationError(
                    f"Unknown PayPal schedule dimension {dimension!r} in {raw_id!r}.",
                    schedule_id=raw_id,
                    dimension=dimension,
                )
        return cls(raw_id=raw_id, stem=stem, applies_to_markets=markets, pricing_plan=pricing_plan)


class _PayPalScheduleRegistry:
    def __init__(self, derived: PayPalDerivedData) -> None:
        self._fixed_fee = derived.fixed_fee_schedules
        self._international_surcharge = derived.international_surcharge_schedules
        self._maximum_fee = derived.maximum_fee_schedules
        for raw_id in self._fixed_fee:
            _ScheduleIdentity.parse(raw_id)
        for raw_id in self._international_surcharge:
            _ScheduleIdentity.parse(raw_id)
        for raw_id in self._maximum_fee:
            _ScheduleIdentity.parse(raw_id)

    def resolve_fixed(self, raw_id: str) -> PayPalFixedFeeSchedule:
        if raw_id in self._fixed_fee:
            return self._fixed_fee[raw_id]
        raise QuoteNotAvailable(
            "No matching PayPal fixed-fee schedule found.",
            schedule_id=raw_id,
        )

    def resolve_surcharge(self, raw_id: str) -> PayPalInternationalSurchargeSchedule:
        if raw_id in self._international_surcharge:
            return self._international_surcharge[raw_id]
        raise QuoteNotAvailable(
            "No matching PayPal international surcharge schedule found.",
            schedule_id=raw_id,
        )

    def resolve_maximum(self, raw_id: str) -> PayPalMaximumFeeSchedule:
        if raw_id in self._maximum_fee:
            return self._maximum_fee[raw_id]
        raise QuoteNotAvailable(
            "No matching PayPal maximum-fee schedule found.",
            schedule_id=raw_id,
        )


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

    _merge_context_overrides(context, transaction.context)

    # Internal target for applies_to_markets conditions.
    transaction_region = str(context.get("transaction_region", "")).lower()
    if transaction_region == "international":
        context["applies_to_markets_target"] = context.get("customer_country")
    else:
        context["applies_to_markets_target"] = context.get("account_country")

    return context


def _normalize_paypal_conditions(rule: PayPalTransactionFeeRule) -> list[NormalizedCondition]:
    conditions: list[NormalizedCondition] = []
    for dimension, expected in rule.conditions.items():
        if dimension == "amount":
            if isinstance(expected, dict):
                currency = expected.get("currency")
                if currency is not None:
                    conditions.append(NormalizedCondition("amount_currency", "eq", currency))
                operator = str(expected.get("operator", "eq")).lower()
                value = expected.get("value")
                conditions.append(NormalizedCondition("transaction_amount", operator, value))
            continue

        if dimension == "applies_to_markets":
            values = _as_list(expected)
            if any(str(v).lower() == "all_other_markets" for v in values):
                continue
            conditions.append(NormalizedCondition("applies_to_markets_target", "in", values))
            continue

        if dimension == "payment_methods":
            conditions.append(NormalizedCondition("payment_method", "in", _as_list(expected)))
            continue

        if isinstance(expected, list):
            conditions.append(NormalizedCondition(dimension, "in", expected))
        else:
            conditions.append(NormalizedCondition(dimension, "eq", expected))

    return conditions


PAYPAL_API_FIELD_NAMES: dict[str, str] = {
    "product_id": "transaction.product_id",
    "variant_id": "transaction.variant_id",
    "payment_method": "transaction.payment_method",
    "transaction_region": "transaction.transaction_region",
    "payer_region": "transaction.payer_region",
    "surcharge_region": "transaction.surcharge_region",
    "applies_to_markets_target": "customer_country",
    "customer_country": "customer_country",
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
    "amount_currency": "amount.currency",
    "transaction_amount": "amount.value",
}


def _api_field_name(dimension: str) -> str:
    return _api_field_name_lookup(PAYPAL_API_FIELD_NAMES, dimension)


def _specificity(rule: PayPalTransactionFeeRule) -> float:
    score = 0.0
    if rule.variant_id:
        score += 0.5
    for dimension, expected in rule.conditions.items():
        if dimension == "amount":
            score += 1.0
        elif dimension == "applies_to_markets":
            values = _as_list(expected)
            if any(str(v).lower() == "all_other_markets" for v in values):
                score += 1.0
            else:
                score += 1.0 + (1.0 / max(len(values), 1))
        elif dimension == "payment_methods":
            values = _as_list(expected)
            score += 1.0 + (1.0 / max(len(values), 1))
        elif dimension == "pricing_plan":
            score += 2.0
        else:
            score += 1.0
    return score


def _rule_percentage(rule: PayPalTransactionFeeRule) -> Decimal | None:
    if rule.percentage is not None:
        return rule.percentage
    for comp in rule.fee_components:
        if comp.type == "percentage" and comp.value is not None:
            return comp.value
    return None


def _component_schedule_id(rule: PayPalTransactionFeeRule, schedule_type: str) -> str | None:
    for comp in rule.fee_components:
        if comp.type == schedule_type:
            return comp.schedule_id
    return None


def _resolve_fixed_amount(
    rule: PayPalTransactionFeeRule,
    schedule_registry: _PayPalScheduleRegistry,
    currency: str,
    raise_on_missing_currency: bool = True,
) -> tuple[Decimal | None, str | None]:
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
        fixed_schedule = schedule_registry.resolve_fixed(fixed_schedule_name)
        raw = fixed_schedule.entries.get(currency)
        if raw is None:
            if raise_on_missing_currency:
                raise QuoteNotAvailable(
                    "No PayPal fixed fee is published for the transaction currency.",
                    rule_id=rule.id,
                    currency=currency,
                    schedule=fixed_schedule_name,
                )
        else:
            schedule_fixed = to_decimal(raw, "fixed fee")
            if fixed_amount is None:
                fixed_amount = Decimal("0")
                fixed_currency = currency
            fixed_amount += schedule_fixed

    return fixed_amount, fixed_currency or currency


def _resolve_maximum_amount(
    rule: PayPalTransactionFeeRule,
    schedule_registry: _PayPalScheduleRegistry,
    currency: str,
    raise_on_missing_currency: bool = True,
) -> Decimal | None:
    max_schedule_name = rule.maximum_fee_schedule or _component_schedule_id(rule, "maximum_fee_schedule")
    if max_schedule_name:
        max_schedule = schedule_registry.resolve_maximum(max_schedule_name)
        raw = max_schedule.entries.get(currency)
        if raw is None:
            if raise_on_missing_currency:
                raise QuoteNotAvailable(
                    "No PayPal maximum fee is published for the transaction currency.",
                    rule_id=rule.id,
                    currency=currency,
                    schedule=max_schedule_name,
                )
            return None
        return to_decimal(raw, "maximum fee")
    return None


def _resolve_surcharge_rate(
    rule: PayPalTransactionFeeRule,
    schedule_registry: _PayPalScheduleRegistry,
    payer_region: str | None,
) -> Decimal | None:
    surcharge_schedule_name = rule.international_surcharge_schedule or _component_schedule_id(
        rule, "international_surcharge_schedule"
    )
    if surcharge_schedule_name and payer_region:
        surcharge_schedule = schedule_registry.resolve_surcharge(surcharge_schedule_name)
        for entry in surcharge_schedule.entries:
            if entry.payer_region.upper() == payer_region.upper():
                if entry.percentage_points is None:
                    return None
                return to_decimal(entry.percentage_points, "surcharge percentage")
    return None


def _rule_signature(
    rule: PayPalTransactionFeeRule,
    schedule_registry: _PayPalScheduleRegistry,
    currency: str,
    payer_region: str | None,
) -> tuple[Any, ...]:
    percentage = _rule_percentage(rule)
    fixed_amount, _ = _resolve_fixed_amount(rule, schedule_registry, currency, raise_on_missing_currency=False)
    maximum_amount = _resolve_maximum_amount(rule, schedule_registry, currency, raise_on_missing_currency=False)
    surcharge_rate = _resolve_surcharge_rate(rule, schedule_registry, payer_region)
    return (percentage, fixed_amount, maximum_amount, surcharge_rate)


def _compile_rule(
    rule: PayPalTransactionFeeRule,
    schedule_registry: _PayPalScheduleRegistry,
    request: PayPalQuoteRequest,
    context: dict[str, Any],
    base_static_model: ExecutableFeeRule,
    surcharge_static_model: ExecutableFeeRule,
) -> list[ExecutableFeeRule]:
    currency = request.amount.currency
    payer_region = context.get("payer_region") or context.get("surcharge_region")

    percentage_raw = _rule_percentage(rule)
    percentage = to_decimal(percentage_raw, "percentage") if percentage_raw is not None else None

    fixed_amount, fixed_currency = _resolve_fixed_amount(rule, schedule_registry, currency)
    maximum_amount = _resolve_maximum_amount(rule, schedule_registry, currency)
    surcharge_rate = _resolve_surcharge_rate(rule, schedule_registry, payer_region)

    return _build_executable_rules(
        rule=rule,
        request=request,
        payer_region=payer_region,
        percentage=percentage,
        fixed_amount=fixed_amount,
        fixed_currency=fixed_currency,
        maximum_amount=maximum_amount,
        surcharge_rate=surcharge_rate,
        base_static_model=base_static_model,
        surcharge_static_model=surcharge_static_model,
    )


def _build_executable_rules(
    rule: PayPalTransactionFeeRule,
    request: PayPalQuoteRequest,
    payer_region: str | None,
    percentage: Decimal | None,
    fixed_amount: Decimal | None,
    fixed_currency: str | None,
    maximum_amount: Decimal | None,
    surcharge_rate: Decimal | None,
    base_static_model: ExecutableFeeRule,
    surcharge_static_model: ExecutableFeeRule,
) -> list[ExecutableFeeRule]:
    executable_rules: list[ExecutableFeeRule] = []
    currency = request.amount.currency

    if percentage is not None or fixed_amount is not None or maximum_amount is not None:
        executable_rules.append(
            base_static_model.model_copy(
                update={
                    "percentage": percentage,
                    "fixed_amount": fixed_amount,
                    "fixed_currency": fixed_currency or currency,
                    "maximum_amount": maximum_amount,
                    "currency": currency,
                }
            )
        )

    if surcharge_rate is not None:
        executable_rules.append(
            surcharge_static_model.model_copy(
                update={
                    "rule_id": (
                        f"paypal:{request.account_country}:{rule.id}:"
                        f"{rule.variant_id or 'default'}:surcharge:{payer_region}"
                    ),
                    "label": f"International surcharge ({payer_region})",
                    "percentage": surcharge_rate,
                    "currency": currency,
                    "metadata": {
                        "product_id": rule.id,
                        "variant_id": rule.variant_id,
                        "payer_region": payer_region,
                    },
                }
            )
        )

    if not executable_rules:
        raise QuoteNotAvailable(
            "The selected PayPal fee rule has no calculable components.",
            rule_id=rule.id,
        )

    return executable_rules


def _classify_rule(rule: PayPalTransactionFeeRule) -> str:
    supported = all(comp.type in SUPPORTED_FEE_COMPONENT_TYPES for comp in rule.fee_components)
    status = rule.calculation_status
    if status == "calculable" and supported:
        return "calculable"
    if status == "included":
        return "included"
    if status == "custom_pricing":
        return "custom_pricing"
    if status == "unsupported" or not supported:
        return "unsupported"
    if status == "non_calculable":
        return "non_calculable"
    return "non_calculable"


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
        self._schedule_registries = {
            code: _PayPalScheduleRegistry(country.derived) for code, country in self._countries.items()
        }

        self._rule_indexes: dict[str, dict[str, dict[str, list[PayPalTransactionFeeRule]]]] = {}
        self._rule_specificity: dict[int, float] = {}
        self._rule_static_models: dict[tuple[str, str, str], tuple[ExecutableFeeRule, ExecutableFeeRule]] = {}
        self._compiled_plan_templates: dict[str, CompiledFeePlan] = {}

        for code, country in self._countries.items():
            index_entry = self._index_map.get(code)
            self._compiled_plan_templates[code] = CompiledFeePlan(
                provider=self.provider_id,
                market=code,
                currency="",
                rules=[],
                schema_version=self.core.schema_version,
                content_sha256=index_entry.content_sha256 if index_entry else None,
                source_urls=[],
                source_updated_at=index_entry.source_updated_at if index_entry else None,
                data_ref=self.data_ref,
                product_id=None,
                variant_id=None,
            )
            index_by_product: dict[str, dict[str, list[PayPalTransactionFeeRule]]] = {}
            for rule in country.derived.transaction_fee_rules:
                product_id = rule.id.lower()
                variant_id = (rule.variant_id or "default").lower()
                index_by_product.setdefault(product_id, {}).setdefault(variant_id, []).append(rule)
                self._rule_specificity[id(rule)] = _specificity(rule)

                source_url = self._resolve_source_url(rule, code)
                base_static_model = ExecutableFeeRule(
                    rule_id=f"paypal:{code}:{rule.id}:{rule.variant_id or 'default'}:base",
                    label=rule.label or rule.id,
                    component_type="processing",
                    behavior="base",
                    classification_status=rule.calculation_status,
                    exactness="exact",
                    confidence=1.0,
                    source_url=source_url,
                    metadata={
                        "product_id": rule.id,
                        "variant_id": rule.variant_id,
                    },
                )
                surcharge_static_model = ExecutableFeeRule(
                    rule_id="",
                    label="",
                    component_type="surcharge",
                    behavior="additive",
                    classification_status=rule.calculation_status,
                    exactness="exact",
                    confidence=1.0,
                    source_url=source_url,
                    metadata={
                        "product_id": rule.id,
                        "variant_id": rule.variant_id,
                    },
                )
                self._rule_static_models[(code, rule.id, rule.variant_id or "default")] = (
                    base_static_model,
                    surcharge_static_model,
                )

            self._rule_indexes[code] = index_by_product

    @classmethod
    def from_paths(
        cls,
        path: str,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> PayPalProvider:
        core_path = load_json(f"{path}/json/core-fees.json")
        index_path = load_json(f"{path}/json/index.json")
        core_document = adapt_paypal_core_document(core_path)
        index_document = adapt_paypal_index_document(index_path)
        if validate_schema:
            from payment_fee.data import validate_json_schema

            validate_json_schema(core_document, f"{path}/schemas/core-fees-v1.schema.json", "paypal-core")
            validate_json_schema(index_document, f"{path}/schemas/index-v1.schema.json", "paypal-index")
        core = PayPalCoreFees.model_validate(core_document)
        index = PayPalIndex.model_validate(index_document)
        _check_schema_version(core, SUPPORTED_SCHEMA_VERSIONS, "PayPal")
        return cls(core=core, index=index, data_ref=data_ref)

    @classmethod
    def from_documents(
        cls,
        core: dict[str, Any],
        index: dict[str, Any] | None = None,
        schemas: dict[str, Any] | None = None,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> PayPalProvider:
        core_document = adapt_paypal_core_document(core)
        index_document = adapt_paypal_index_document(index) if index else None
        if validate_schema:
            from payment_fee.data import validate_json_schema

            if schemas is None or "core" not in schemas:
                raise DatasetValidationError(
                    "PayPal core schema is required for document validation.",
                    schema="core",
                )
            validate_json_schema(core_document, schemas["core"], "paypal-core")
            if index_document is not None:
                if "index" not in schemas:
                    raise DatasetValidationError(
                        "PayPal index schema is required for document validation.",
                        schema="index",
                    )
                validate_json_schema(index_document, schemas["index"], "paypal-index")
        core_model = PayPalCoreFees.model_validate(core_document)
        index_model = PayPalIndex.model_validate(index_document) if index_document else None
        _check_schema_version(core_model, SUPPORTED_SCHEMA_VERSIONS, "PayPal")
        return cls(core=core_model, index=index_model, data_ref=data_ref)

    def _country(self, code: str) -> PayPalCountryEntry:
        code = code.upper()
        country = self._countries.get(code)
        if country is None:
            raise UnknownMarket(self.provider_id, code)
        return country

    def _resolve_product_rules(
        self, context: dict[str, Any], account_country: str
    ) -> tuple[list[PayPalTransactionFeeRule], str, str | None]:
        product_id = context.get("product_id")
        index_by_product = self._rule_indexes[account_country.upper()]
        if not product_id:
            available = sorted(index_by_product.keys())
            raise InsufficientTransactionContext(
                ["transaction.product_id"],
                provider=self.provider_id,
                market=account_country,
                available_product_ids=available,
            )

        product_id = str(product_id).lower()
        variant_rules = index_by_product.get(product_id, {})

        variant_id = context.get("variant_id")
        if variant_id:
            variant_id = str(variant_id).lower()
            product_rules = variant_rules.get(variant_id, [])
        else:
            product_rules = [r for rules in variant_rules.values() for r in rules]

        if not product_rules:
            raise QuoteNotAvailable(
                "The requested PayPal product/variant is not classified for this market.",
                market=account_country,
                product_id=product_id,
                variant_id=variant_id,
            )

        return product_rules, product_id, variant_id

    def _check_surcharge_region_context(
        self,
        selected: PayPalTransactionFeeRule,
        schedule_registry: _PayPalScheduleRegistry,
        payer_region: str | None,
        transaction_region: Any,
        account_country: str,
    ) -> None:
        surcharge_schedule_name = (
            selected.international_surcharge_schedule
            or _component_schedule_id(selected, "international_surcharge_schedule")
            or ""
        )
        if surcharge_schedule_name and payer_region is None and transaction_region != "domestic":
            surcharge_schedule = schedule_registry.resolve_surcharge(surcharge_schedule_name)
            available_regions = [e.payer_region for e in surcharge_schedule.entries]
            raise InsufficientTransactionContext(
                ["transaction.payer_region", "transaction.surcharge_region"],
                provider=self.provider_id,
                market=account_country,
                available_surcharge_regions=sorted(set(available_regions)),
            )

    def _resolve_source_url(self, selected: PayPalTransactionFeeRule, account_country: str) -> str | None:
        if selected.source:
            return selected.source.canonical_url or selected.source.requested_url
        index_entry = self._index_map.get(account_country.upper())
        return index_entry.source_url if index_entry else None

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan:
        if not isinstance(request, PayPalQuoteRequest):
            raise TypeError(f"Expected PayPalQuoteRequest, got {type(request).__name__}")
        _ = self._country(request.account_country)
        schedule_registry = self._schedule_registries[request.account_country.upper()]
        context = _build_paypal_context(request)

        product_rules, product_id, variant_id = self._resolve_product_rules(context, request.account_country)
        candidates = [
            (rule, _normalize_paypal_conditions(rule), self._rule_specificity[id(rule)]) for rule in product_rules
        ]
        payer_region = context.get("payer_region") or context.get("surcharge_region")

        def _signature(rule: PayPalTransactionFeeRule) -> tuple[Any, ...]:
            return _rule_signature(rule, schedule_registry, request.amount.currency, payer_region)

        selected, _ = compile_generic(
            candidates,
            context,
            request.account_country,
            self.provider_id,
            api_field_name=_api_field_name,
            is_evaluable=lambda r: r.calculation_status == "calculable",
            select_filter=lambda r: r.calculation_status == "calculable",
            financial_signature=_signature,
            rule_id=lambda r: r.id,
            sort_key=lambda r: (r.id, r.variant_id or ""),
            classification_status=lambda r: r.calculation_status,
            require_all_evaluable=True,
            check_more_specific_missing=False,
            not_calculable_message="A selected PayPal rule is not calculable.",
            no_selectable_message="No evaluable PayPal fee rule matched the supplied context.",
            error_context={"product_id": product_id, "variant_id": variant_id},
        )
        self._check_surcharge_region_context(
            selected, schedule_registry, payer_region, context.get("transaction_region"), request.account_country
        )

        source_url = self._resolve_source_url(selected, request.account_country)
        base_static_model, surcharge_static_model = self._rule_static_models[
            (request.account_country.upper(), selected.id, selected.variant_id or "default")
        ]
        executable_rules = _compile_rule(
            selected, schedule_registry, request, context, base_static_model, surcharge_static_model
        )

        assumptions = [
            "Public standard pricing was used; negotiated merchant pricing is not represented.",
            "The published dataset does not encode provider settlement rounding, so "
            "standard currency rounding is used.",
        ]

        index_entry = self._index_map.get(request.account_country.upper())
        plan_template = self._compiled_plan_templates[request.account_country.upper()]
        return plan_template.model_copy(
            update={
                "market": request.account_country,
                "currency": request.amount.currency,
                "rules": executable_rules,
                "assumptions": assumptions,
                "content_sha256": index_entry.content_sha256 if index_entry else None,
                "source_urls": [source_url] if source_url else [],
                "source_updated_at": index_entry.source_updated_at if index_entry else None,
                "product_id": selected.id,
                "variant_id": selected.variant_id,
            }
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
        acc = CapabilityAccumulator(self.provider_id, account_country)

        for rule in derived.transaction_fee_rules:
            product_id = rule.id
            variant_id = rule.variant_id
            bucket = _classify_rule(rule)

            fee_shapes: list[str] = []
            currencies: list[str] = []
            for comp in rule.fee_components:
                if comp.type in SUPPORTED_FEE_COMPONENT_TYPES:
                    fee_shapes.append(comp.type)
                if comp.type == "fixed_amount" and comp.currency:
                    currencies.append(comp.currency)

            payment_methods: list[str] = []
            if "payment_methods" in rule.conditions:
                payment_methods = [str(v) for v in _as_list(rule.conditions["payment_methods"])]

            acc.add_rule(
                product_id=product_id,
                variant_id=variant_id,
                payment_methods=payment_methods,
                fee_shapes=fee_shapes,
                currencies=currencies,
                conditions=_normalize_paypal_conditions(rule),
                classification_bucket=bucket,
                bucket_variant_id=rule.variant_id or "default",
            )

        for schedule in derived.fixed_fee_schedules.values():
            acc.add_currencies(schedule.entries.keys())

        for schedule in derived.maximum_fee_schedules.values():
            acc.add_currencies(schedule.entries.keys())

        index_entry = self._index_map.get(account_country.upper())
        return acc.to_capability_info(
            quotable=bool(derived.transaction_fee_rules),
            required_context=sorted(_required_context(derived)),
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

    def _compile_single_rule_for_audit(
        self,
        rule: PayPalTransactionFeeRule,
        context: Any,
    ) -> list[ExecutableFeeRule]:
        """Compile a single PayPal rule for contract auditing.

        This is the explicit audit hook called by ``audit.py`` so it does not
        have to reach into provider internals.
        """
        request = context
        schedule_registry = self._schedule_registries[request.account_country.upper()]
        ctx = _build_paypal_context(request)
        base_static_model, surcharge_static_model = self._rule_static_models[
            (request.account_country.upper(), rule.id, rule.variant_id or "default")
        ]
        return _compile_rule(rule, schedule_registry, request, ctx, base_static_model, surcharge_static_model)
