from __future__ import annotations

from decimal import Decimal
from typing import Any

from payment_fee.calculator import to_decimal
from payment_fee.data import load_json
from payment_fee.errors import (
    InsufficientTransactionContext,
    ProviderDataUnavailable,
    QuoteNotAvailable,
    UnknownMarket,
    UnsupportedFeeShape,
)
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema, StripeQuoteRequest
from payment_fee.providers.base import (
    SUPPORTED_OPERATORS as _SUPPORTED_OPERATORS,
)
from payment_fee.providers.base import (
    SUPPORTED_SCHEMA_VERSIONS,
    CapabilityAccumulator,
    NormalizedCondition,
    _api_field_name_lookup,
    _check_schema_version,
    _evaluate_condition,
    _merge_context_overrides,
    compile_generic,
)
from payment_fee.providers.stripe.models import (
    StripeCoreFees,
    StripeFeeComponent,
    StripeIndex,
    StripeIndexMarket,
    StripeRule,
)
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule

SUPPORTED_OPERATORS = _SUPPORTED_OPERATORS

SUPPORTED_COMPONENT_TYPES = {
    "fixed_amount",
    "percentage",
    "percentage_surcharge",
    "fixed_surcharge",
    "minimum_fee",
    "maximum_fee",
}

EVALUABLE_CLASSIFICATION_STATUSES = {"calculable_rule", "free", "included"}


def _build_stripe_context(request: StripeQuoteRequest) -> dict[str, Any]:
    t = request.transaction
    context: dict[str, Any] = {
        "account_country": request.account_country.upper(),
        "customer_country": request.customer_country.upper() if request.customer_country else None,
        "amount_currency": request.amount.currency.upper(),
        "transaction_amount": request.amount.value,
        "presentment_currency": request.amount.currency.upper(),
        "settlement_currency": request.settlement_currency,
        "product_id": t.product_id,
        "variant_id": t.variant_id,
        "payment_method": t.payment_method,
        "payment_method_variant": t.payment_method_variant,
        "channel": t.channel,
        "pricing_plan": t.pricing_plan,
        "pricing_tier": t.pricing_tier,
        "payer": t.payer,
        "unit": t.unit or "per_transaction",
        "currency_conversion_required": t.currency_conversion_required,
        "recurring": t.recurring,
        "billing_type": t.billing_type,
        "transaction_region": t.transaction_region,
        "cross_border": t.cross_border,
        "integration_type": t.integration_type,
        "product_feature": t.product_feature,
        "contract_length": t.contract_length,
        "feature_enabled": t.feature_enabled,
        "dispute_state": t.dispute_state,
    }

    if t.card:
        for attr in ("origin", "region", "type", "network", "tier", "entry_mode"):
            value = getattr(t.card, attr)
            if value is not None:
                context[f"card_{attr}"] = value

    if t.settlement:
        if t.settlement.currency:
            context["settlement_currency"] = t.settlement.currency.upper()
        if t.settlement.timing:
            context["settlement_timing"] = t.settlement.timing

    if t.bank:
        if t.bank.validation:
            context["bank_account_validation"] = t.bank.validation
        if t.bank.transfer_type:
            context["bank_transfer_type"] = t.bank.transfer_type

    # Default to successful transactions; allow transaction.context to override.
    context["success"] = True
    if "success" in t.context:
        context["success"] = t.context["success"]

    _merge_context_overrides(context, t.context)

    return context


def _normalize_conditions(rule: StripeRule) -> list[NormalizedCondition]:
    conditions: list[NormalizedCondition] = []

    top_level_fields = [
        ("account_country", rule.account_country),
        ("payment_method", rule.payment_method),
        ("payment_method_variant", getattr(rule, "payment_method_variant", None)),
        ("product_id", rule.product_id),
        ("variant_id", rule.variant_id),
        ("channel", rule.channel),
        ("card_origin", rule.card_origin),
        ("card_region", rule.card_region),
        ("card_tier", rule.card_tier),
        ("card_type", rule.card_type),
        ("card_network", rule.card_network),
        ("card_entry_mode", rule.card_entry_mode),
        ("customer_country", rule.customer_country),
        ("presentment_currency", rule.presentment_currency),
        ("settlement_currency", rule.settlement_currency),
        ("settlement_timing", rule.settlement_timing),
        ("currency_conversion_required", rule.currency_conversion_required),
        ("recurring", rule.recurring),
        ("billing_type", rule.billing_type),
        ("pricing_plan", rule.pricing_plan),
        ("pricing_tier", rule.pricing_tier),
        ("product_feature", rule.product_feature),
        ("integration_type", rule.integration_type),
        ("contract_length", rule.contract_length),
        ("dispute_state", rule.dispute_state),
        ("transaction_region", rule.transaction_region),
        ("transaction_type", rule.transaction_type),
        ("cross_border", rule.cross_border),
        ("feature_enabled", rule.feature_enabled),
        ("payer", rule.payer),
        ("success", rule.success),
        ("bank_account_validation", getattr(rule, "bank_account_validation", None)),
        ("bank_transfer_type", rule.bank_transfer_type),
        ("fee_type", rule.fee_type),
    ]

    for dimension, value in top_level_fields:
        if value is not None:
            conditions.append(NormalizedCondition(dimension, "eq", value))

    if rule.transaction_amount_min is not None:
        conditions.append(NormalizedCondition("transaction_amount", "gte", rule.transaction_amount_min))
    if rule.transaction_amount_max is not None:
        conditions.append(NormalizedCondition("transaction_amount", "lte", rule.transaction_amount_max))

    for condition in rule.conditions:
        op = str(condition.operator).lower() if condition.operator else "eq"
        conditions.append(NormalizedCondition(condition.dimension, op, condition.value))

    return conditions


def _specificity(rule: StripeRule) -> int:
    return len(_normalize_conditions(rule))


def _rule_financial_signature(
    rule: StripeRule,
    currency: str,
    compiled: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    if compiled is None:
        compiled = _compile_stripe_components(rule, currency)
    return (
        compiled.get("percentage"),
        compiled.get("fixed_amount"),
        compiled.get("minimum_amount"),
        compiled.get("maximum_amount"),
        compiled.get("behavior"),
    )


STRIPE_API_FIELD_NAMES: dict[str, str] = {
    "payment_method": "transaction.payment_method",
    "payment_method_variant": "transaction.payment_method_variant",
    "product_id": "transaction.product_id",
    "variant_id": "transaction.variant_id",
    "channel": "transaction.channel",
    "card_origin": "transaction.card.origin",
    "card_region": "transaction.card.region",
    "card_tier": "transaction.card.tier",
    "card_type": "transaction.card.type",
    "card_network": "transaction.card.network",
    "card_entry_mode": "transaction.card.entry_mode",
    "account_country": "account_country",
    "customer_country": "customer_country",
    "amount_currency": "amount.currency",
    "presentment_currency": "amount.currency",
    "settlement_currency": "settlement_currency",
    "settlement_timing": "transaction.settlement.timing",
    "currency_conversion_required": "transaction.currency_conversion_required",
    "recurring": "transaction.recurring",
    "billing_type": "transaction.billing_type",
    "pricing_plan": "transaction.pricing_plan",
    "pricing_tier": "transaction.pricing_tier",
    "product_feature": "transaction.product_feature",
    "integration_type": "transaction.integration_type",
    "contract_length": "transaction.contract_length",
    "dispute_state": "transaction.dispute_state",
    "transaction_region": "transaction.transaction_region",
    "transaction_type": "transaction.context.transaction_type",
    "cross_border": "transaction.cross_border",
    "feature_enabled": "transaction.feature_enabled",
    "payer": "transaction.payer",
    "unit": "transaction.unit",
    "success": "transaction.context.success",
    "bank_account_validation": "transaction.bank.validation",
    "bank_transfer_type": "transaction.bank.transfer_type",
    "fee_type": "transaction.context.fee_type",
    "transaction_amount": "amount.value",
}


def _api_field_name(dimension: str) -> str:
    return _api_field_name_lookup(STRIPE_API_FIELD_NAMES, dimension)


def _is_evaluable(rule: StripeRule) -> bool:
    return rule.classification_status in EVALUABLE_CLASSIFICATION_STATUSES


def _synthesize_legacy_components(rule: StripeRule, currency: str) -> list[StripeFeeComponent]:
    """Create synthetic fee components from legacy top-level rule fields."""
    components = list(rule.fee_components)
    if not components and rule.basis_points is not None:
        components.append(StripeFeeComponent(type="percentage", basis_points=rule.basis_points))
    if not components and rule.fixed_amount is not None:
        components.append(
            StripeFeeComponent(
                type="fixed_amount",
                amount=rule.fixed_amount,
                currency=rule.fixed_currency or currency,
            )
        )
    if rule.minimum_amount is not None:
        components.append(
            StripeFeeComponent(
                type="minimum_fee",
                amount=rule.minimum_amount,
                currency=rule.fixed_currency or currency,
            )
        )
    if rule.maximum_amount is not None:
        components.append(
            StripeFeeComponent(
                type="maximum_fee",
                amount=rule.maximum_amount,
                currency=rule.fixed_currency or currency,
            )
        )
    return components


def _aggregate_components(
    components: list[StripeFeeComponent],
    currency: str,
    rule_id: str,
) -> dict[str, Any]:
    """Add up base/additive percentages and fixed amounts and capture min/max."""
    base_percentage = Decimal("0")
    base_fixed = Decimal("0")
    additive_percentage = Decimal("0")
    additive_fixed = Decimal("0")
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    unsupported: list[str] = []

    for comp in components:
        if comp.type == "percentage":
            base_percentage += _component_rate(comp)
        elif comp.type == "percentage_surcharge":
            additive_percentage += _component_rate(comp)
        elif comp.type == "fixed_amount":
            base_fixed += _component_fixed(comp, currency, rule_id)
        elif comp.type == "fixed_surcharge":
            additive_fixed += _component_fixed(comp, currency, rule_id)
        elif comp.type == "minimum_fee":
            minimum_amount = _component_fixed(comp, currency, rule_id)
        elif comp.type == "maximum_fee":
            maximum_amount = _component_fixed(comp, currency, rule_id)
        else:
            unsupported.append(comp.type)

    if unsupported:
        raise UnsupportedFeeShape(
            "Unsupported Stripe fee component type.",
            rule_id=rule_id,
            types=unsupported,
        )

    return {
        "base_percentage": base_percentage,
        "base_fixed": base_fixed,
        "additive_percentage": additive_percentage,
        "additive_fixed": additive_fixed,
        "minimum_amount": minimum_amount,
        "maximum_amount": maximum_amount,
    }


def _apply_behavior(rule: StripeRule, aggregated: dict[str, Any]) -> dict[str, Any]:
    """Pick base or additive totals based on the rule's behavior."""
    base_percentage = aggregated["base_percentage"]
    base_fixed = aggregated["base_fixed"]
    additive_percentage = aggregated["additive_percentage"]
    additive_fixed = aggregated["additive_fixed"]
    minimum_amount = aggregated["minimum_amount"]
    maximum_amount = aggregated["maximum_amount"]

    behavior = "included" if rule.classification_status in {"free", "included"} else rule.behavior

    if behavior == "additive":
        percentage = additive_percentage or None
        fixed_amount = additive_fixed or None
    elif behavior == "included":
        percentage = None
        fixed_amount = None
    else:
        percentage = base_percentage or None
        fixed_amount = base_fixed or None

    return {
        "percentage": percentage,
        "fixed_amount": fixed_amount,
        "minimum_amount": minimum_amount,
        "maximum_amount": maximum_amount,
        "behavior": behavior,
    }


def _compile_stripe_components(rule: StripeRule, currency: str) -> dict[str, Any]:
    components = _synthesize_legacy_components(rule, currency)
    aggregated = _aggregate_components(components, currency, rule.rule_id)
    return _apply_behavior(rule, aggregated)


def _component_rate(comp: StripeFeeComponent) -> Decimal:
    if comp.basis_points is not None:
        return to_decimal(comp.basis_points, "basis points") / Decimal("100")
    if comp.value is not None:
        return to_decimal(comp.value, "percentage")
    raise UnsupportedFeeShape(
        "Percentage component missing basis_points and value.",
        component=comp.type,
    )


def _component_fixed(comp: StripeFeeComponent, currency: str, rule_id: str) -> Decimal:
    if comp.amount is None:
        raise UnsupportedFeeShape(
            "Fixed component missing amount.",
            component=comp.type,
            rule_id=rule_id,
        )
    comp_currency = (comp.currency or currency).upper()
    if comp_currency != currency.upper():
        raise QuoteNotAvailable(
            "A selected Stripe fee rule uses a fixed amount in a different currency.",
            rule_id=rule_id,
            component_currency=comp_currency,
            transaction_currency=currency,
        )
    return to_decimal(comp.amount, comp.type)


def _executable_from_rule(
    rule: StripeRule,
    currency: str,
    compiled: dict[str, Any] | None,
    template: dict[str, Any],
) -> ExecutableFeeRule:
    if compiled is None:
        compiled = _compile_stripe_components(rule, currency)
    return ExecutableFeeRule(
        **template,
        percentage=compiled.get("percentage"),
        fixed_amount=compiled.get("fixed_amount"),
        fixed_currency=currency,
        minimum_amount=compiled.get("minimum_amount"),
        maximum_amount=compiled.get("maximum_amount"),
        currency=currency,
    )


def _sanitize_index_document(index: dict[str, Any] | None) -> dict[str, Any] | None:
    if not index:
        return index
    for market in index.get("markets", []):
        market.pop("schema_version", None)
    return index


def _classify_rule(rule: StripeRule) -> str:
    status = rule.classification_status
    if status in {"free", "included"}:
        return "included"
    if status == "custom_pricing":
        return "custom_pricing"
    if status == "unsupported":
        return "unsupported"
    if status == "non_calculable":
        return "non_calculable"
    if status == "calculable_rule":
        for comp in rule.fee_components:
            if comp.type not in SUPPORTED_COMPONENT_TYPES:
                return "non_calculable"
        return "calculable"
    return "non_calculable"


def _required_context(market: Any) -> set[str]:
    required: set[str] = set()
    for rule in market.rules:
        if not _is_evaluable(rule):
            continue
        for condition in _normalize_conditions(rule):
            if condition.dimension == "account_country":
                continue
            required.add(_api_field_name(condition.dimension))
    return required


def _stripe_request_schema(cap: CapabilityInfo) -> dict[str, Any]:
    context_properties: dict[str, Any] = {}
    for dimension in cap.condition_dimensions:
        values = cap.allowed_values.get(dimension)
        if values:
            context_properties[dimension] = {"enum": values}
        else:
            context_properties[dimension] = {"type": ["string", "number", "boolean", "null", "array"]}

    product_enum = {"enum": cap.product_ids} if cap.product_ids else {"type": "string"}
    variant_enum = {"enum": cap.variants} if cap.variants else {"type": "string"}
    payment_method_enum = {"enum": cap.payment_methods} if cap.payment_methods else {"type": "string"}

    return {
        "type": "object",
        "properties": {
            "provider": {"enum": ["stripe"]},
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
                "properties": {
                    "product_id": product_enum,
                    "variant_id": variant_enum,
                    "payment_method": payment_method_enum,
                    "payment_method_variant": {"type": "string"},
                    "channel": {"type": "string"},
                    "pricing_plan": {"type": "string"},
                    "pricing_tier": {"type": "string"},
                    "payer": {"type": "string"},
                    "unit": {"type": "string"},
                    "currency_conversion_required": {"type": "boolean"},
                    "recurring": {"type": "boolean"},
                    "billing_type": {"type": "string"},
                    "transaction_region": {"type": "string"},
                    "cross_border": {"type": "boolean"},
                    "integration_type": {"type": "string"},
                    "product_feature": {"type": "string"},
                    "contract_length": {"type": "string"},
                    "feature_enabled": {"type": "string"},
                    "dispute_state": {"type": "string"},
                    "card": {
                        "type": "object",
                        "properties": {
                            "origin": {"type": "string"},
                            "region": {"type": "string"},
                            "type": {"type": "string"},
                            "network": {"type": "string"},
                            "tier": {"type": "string"},
                            "entry_mode": {"type": "string"},
                        },
                    },
                    "settlement": {
                        "type": "object",
                        "properties": {
                            "currency": {"type": "string"},
                            "timing": {"type": "string"},
                        },
                    },
                    "bank": {
                        "type": "object",
                        "properties": {
                            "validation": {"type": "string"},
                            "transfer_type": {"type": "string"},
                        },
                    },
                    "context": {
                        "type": "object",
                        "properties": context_properties,
                        "additionalProperties": {"type": ["string", "number", "boolean", "null", "array"]},
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["provider", "amount", "account_country", "transaction"],
    }


class StripeProvider:
    provider_id = "stripe"

    def __init__(
        self,
        core: StripeCoreFees,
        index: StripeIndex | None = None,
        data_ref: str | None = None,
    ) -> None:
        self.core = core
        self.index = index
        self.data_ref = data_ref
        self._markets = {m.account_country.upper(): m for m in core.markets}
        self._index_map: dict[str, StripeIndexMarket] = {}
        if index:
            self._index_map = {im.account_country.upper(): im for im in index.markets}

        self._market_candidates: dict[str, list[tuple[StripeRule, list[NormalizedCondition], int]]] = {}
        self._market_additive_candidates: dict[str, list[tuple[StripeRule, list[NormalizedCondition], int]]] = {}
        self._rule_templates: dict[tuple[str, str], dict[str, Any]] = {}
        self._rule_template_by_id: dict[str, dict[str, Any]] = {}

        for code, market in self._markets.items():
            candidates: list[tuple[StripeRule, list[NormalizedCondition], int]] = []
            additive: list[tuple[StripeRule, list[NormalizedCondition], int]] = []
            for rule in market.rules:
                conditions = _normalize_conditions(rule)
                candidate = (rule, conditions, len(conditions))
                candidates.append(candidate)
                if rule.behavior == "additive":
                    additive.append(candidate)

                behavior = "included" if rule.classification_status in {"free", "included"} else rule.behavior
                component_type = (
                    "surcharge"
                    if behavior == "additive"
                    else "included"
                    if behavior in {"free", "included"}
                    else "processing"
                )
                label = rule.label or rule.name or rule.rule_id
                template = {
                    "rule_id": rule.rule_id,
                    "label": label,
                    "component_type": component_type,
                    "behavior": behavior,
                    "payer": rule.payer,
                    "unit": rule.unit,
                    "classification_status": rule.classification_status,
                    "confidence": rule.confidence,
                    "exactness": rule.exactness,
                    "source_url": rule.source_url,
                    "metadata": {
                        "product_id": rule.product_id,
                        "variant_id": rule.variant_id,
                    },
                }
                self._rule_templates[(code, rule.rule_id)] = template
                self._rule_template_by_id.setdefault(rule.rule_id, template)

            self._market_candidates[code] = candidates
            self._market_additive_candidates[code] = additive

    @classmethod
    def from_paths(
        cls,
        path: str,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> StripeProvider:
        core_path = load_json(f"{path}/json/core-fees.json")
        index_path = load_json(f"{path}/json/index.json")
        index_path = _sanitize_index_document(index_path)
        if validate_schema:
            from payment_fee.data import validate_json_schema

            validate_json_schema(core_path, f"{path}/schemas/core-fees-v1.schema.json", "stripe-core")
            if index_path is None:
                raise ProviderDataUnavailable("stripe", "index.json is missing or empty")
            validate_json_schema(index_path, f"{path}/schemas/index-v1.schema.json", "stripe-index")
        core = StripeCoreFees.model_validate(core_path)
        index = StripeIndex.model_validate(index_path)
        _check_schema_version(core, SUPPORTED_SCHEMA_VERSIONS, "Stripe")
        return cls(
            core=core,
            index=index,
            data_ref=data_ref,
        )

    @classmethod
    def from_documents(
        cls,
        core: dict[str, Any],
        index: dict[str, Any] | None = None,
        schemas: dict[str, Any] | None = None,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> StripeProvider:
        from payment_fee.errors import DatasetValidationError

        core_document = core
        index_document = _sanitize_index_document(index)
        if validate_schema:
            from payment_fee.data import validate_json_schema

            if schemas is None or "core" not in schemas:
                raise DatasetValidationError(
                    "Stripe core schema is required for document validation.",
                    schema="core",
                )
            validate_json_schema(core_document, schemas["core"], "stripe-core")
            if index_document is not None:
                if "index" not in schemas:
                    raise DatasetValidationError(
                        "Stripe index schema is required for document validation.",
                        schema="index",
                    )
                validate_json_schema(index_document, schemas["index"], "stripe-index")
        core_model = StripeCoreFees.model_validate(core_document)
        index_model = StripeIndex.model_validate(index_document) if index_document else None
        _check_schema_version(core_model, SUPPORTED_SCHEMA_VERSIONS, "Stripe")
        return cls(
            core=core_model,
            index=index_model,
            data_ref=data_ref,
        )

    def _market(self, code: str) -> Any:
        code = code.upper()
        market = self._markets.get(code)
        if market is None:
            raise UnknownMarket(self.provider_id, code)
        return market

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan:
        if not isinstance(request, StripeQuoteRequest):
            raise TypeError(f"Expected StripeQuoteRequest, got {type(request).__name__}")
        _ = self._market(request.account_country)
        context = _build_stripe_context(request)
        currency = request.amount.currency
        country_code = request.account_country.upper()

        compiled_cache: dict[tuple[str, str], dict[str, Any]] = {}
        candidates = self._market_candidates[country_code]
        additive_candidates = self._market_additive_candidates[country_code]

        def _signature(rule: StripeRule) -> tuple[Any, ...]:
            compiled = compiled_cache.setdefault((rule.rule_id, currency), _compile_stripe_components(rule, currency))
            return _rule_financial_signature(rule, currency, compiled)

        selected_base, _ = compile_generic(
            candidates,
            context,
            request.account_country,
            self.provider_id,
            api_field_name=_api_field_name,
            is_evaluable=_is_evaluable,
            select_filter=lambda r: _is_evaluable(r) and r.behavior != "additive",
            financial_signature=_signature,
            rule_id=lambda r: r.rule_id,
            sort_key=lambda r: r.rule_id,
            classification_status=lambda r: r.classification_status,
            unsupported_statuses={"unsupported"},
            not_calculable_message="The most specific matching Stripe fee rule cannot be quoted.",
            no_selectable_message="No evaluable base Stripe fee rule matched the supplied context.",
        )
        base_compiled = compiled_cache.setdefault(
            (selected_base.rule_id, currency), _compile_stripe_components(selected_base, currency)
        )

        additive_rules = self._select_additive_rules(additive_candidates, context, request.account_country)

        base_template = self._rule_templates[(country_code, selected_base.rule_id)]
        rules = [_executable_from_rule(selected_base, currency, base_compiled, base_template)]
        for rule in additive_rules:
            additive_compiled = compiled_cache.setdefault(
                (rule.rule_id, currency), _compile_stripe_components(rule, currency)
            )
            additive_template = self._rule_templates[(country_code, rule.rule_id)]
            rules.append(_executable_from_rule(rule, currency, additive_compiled, additive_template))

        index_entry = self._index_map.get(request.account_country.upper())
        source_urls = list(index_entry.source_urls) if index_entry else []
        if not source_urls and selected_base.source_url:
            source_urls = [selected_base.source_url]

        assumptions = [
            "Public standard pricing was used; negotiated or IC++ pricing is not represented.",
            "The published dataset does not encode provider settlement rounding, so "
            "standard currency rounding is used.",
        ]
        if context.get("success") is True:
            assumptions.append("Assumed a successful transaction for providers that require success.")

        return CompiledFeePlan(
            provider=self.provider_id,
            market=request.account_country,
            currency=currency,
            rules=rules,
            assumptions=assumptions,
            schema_version=self.core.schema_version,
            content_sha256=index_entry.content_sha256 if index_entry else None,
            source_urls=source_urls,
            source_updated_at=index_entry.source_updated_at if index_entry else None,
            data_ref=self.data_ref,
            product_id=selected_base.product_id,
            variant_id=selected_base.variant_id,
        )

    def _select_additive_rules(
        self,
        candidates: list[tuple[StripeRule, list[NormalizedCondition], int]],
        context: dict[str, Any],
        account_country: str,
    ) -> list[StripeRule]:
        selected: list[StripeRule] = []
        for rule, conditions, _specificity in candidates:
            statuses: list[str] = []
            payment_method_missing = False
            conflict = False
            for condition in conditions:
                status = _evaluate_condition(condition, context)
                if status == "conflict":
                    conflict = True
                    break
                if condition.dimension == "payment_method" and status == "missing":
                    payment_method_missing = True
                statuses.append(status)
            if conflict:
                continue
            if payment_method_missing:
                raise InsufficientTransactionContext(
                    [_api_field_name("payment_method")],
                    provider=self.provider_id,
                    market=account_country,
                    candidate_rule_ids=[rule.rule_id],
                )
            if any(s == "missing" for s in statuses):
                continue
            if not _is_evaluable(rule):
                continue
            selected.append(rule)
        return selected

    def markets(self) -> list[MarketInfo]:
        result: list[MarketInfo] = []
        for code, market in sorted(self._markets.items()):
            index = self._index_map.get(code)
            market_code = market.stripe_market_code or code.lower()
            locale = market.locale or (index.locale if index else None)
            status = market.derivation_status or (index.derivation_status if index else "unclassified")
            source_urls = list(index.source_urls) if index else []
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
        market = self._market(account_country)
        acc = CapabilityAccumulator(self.provider_id, account_country)

        for rule in market.rules:
            product_id = rule.product_id
            variant_id = rule.variant_id
            fee_shapes = [comp.type for comp in rule.fee_components]
            currencies: list[str] = []
            for comp in rule.fee_components:
                if comp.currency:
                    currencies.append(comp.currency)
            if rule.fixed_currency:
                currencies.append(rule.fixed_currency)
            bucket = _classify_rule(rule)
            acc.add_rule(
                product_id=product_id,
                variant_id=variant_id,
                payment_methods=[rule.payment_method],
                fee_shapes=fee_shapes,
                currencies=currencies,
                conditions=_normalize_conditions(rule),
                classification_bucket=bucket,
            )

        index_entry = self._index_map.get(account_country.upper())
        return acc.to_capability_info(
            quotable=bool(acc.buckets["calculable"]),
            required_context=sorted(_required_context(market)),
            dataset_status=market.derivation_status or (index_entry.derivation_status if index_entry else "unknown"),
            source_revision=index_entry.content_sha256 if index_entry else None,
        )

    def quote_schema(self, account_country: str) -> QuoteSchema:
        cap = self.capabilities(account_country)
        return QuoteSchema(
            provider=self.provider_id,
            account_country=account_country.upper(),
            request_schema=_stripe_request_schema(cap),
            response_schema={},
        )

    def data_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "schema_version": self.core.schema_version,
            "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
            "market_count": len(self._markets),
            "generated_at": self.core.generated_at,
            "data_ref": self.data_ref,
        }

    def _compile_single_rule_for_audit(
        self,
        rule: StripeRule,
        context: Any,
    ) -> ExecutableFeeRule:
        """Compile a single Stripe rule for contract auditing.

        This is the explicit audit hook called by ``audit.py`` so it does not
        have to reach into provider internals.
        """
        currency = context
        template = self._rule_template_by_id.get(rule.rule_id)
        if template is None:
            raise QuoteNotAvailable("No cached rule template found for audit.", rule_id=rule.rule_id)
        return _executable_from_rule(rule, currency, None, template)
