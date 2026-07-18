from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from payment_fee.calculator import to_decimal
from payment_fee.data import load_json
from payment_fee.errors import (
    AmbiguousFeeRules,
    InsufficientTransactionContext,
    QuoteNotAvailable,
    UnknownMarket,
    UnsupportedFeeShape,
)
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema, StripeQuoteRequest
from payment_fee.providers.stripe.models import (
    StripeCoreFees,
    StripeFeeComponent,
    StripeIndex,
    StripeIndexMarket,
    StripePaymentMethods,
    StripeRule,
)
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule

SUPPORTED_SCHEMA_VERSIONS = {1}


@dataclass
class NormalizedCondition:
    dimension: str
    operator: str
    value: Any


SUPPORTED_OPERATORS = {"eq", "==", "equals", "ne", "!=", "not_equals", "in", "not_in", "nin", "gt", "gte", "lt", "lte"}


def _build_stripe_context(request: StripeQuoteRequest) -> dict[str, Any]:
    t = request.transaction
    context: dict[str, Any] = {
        "account_country": request.account_country,
        "customer_country": request.customer_country,
        "amount_currency": request.amount.currency,
        "transaction_amount": request.amount.value,
        "settlement_currency": request.settlement_currency or (t.settlement.currency if t.settlement else None),
        "presentment_currency": request.amount.currency,
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
        "settlement_timing": t.settlement_timing,
        "bank_account_validation": t.bank_account_validation,
        "fee_type": None,
        "success": True,
    }

    if t.card:
        context["card_origin"] = t.card.origin
        context["card_region"] = t.card.region
        context["card_type"] = t.card.type
        context["card_network"] = t.card.network
        context["card_tier"] = t.card.tier
        context["card_entry_mode"] = t.card.entry_mode

    if t.settlement:
        if t.settlement.currency:
            context["settlement_currency"] = t.settlement.currency
        if t.settlement.timing:
            context["settlement_timing"] = t.settlement.timing

    if t.bank and t.bank.validation:
        context["bank_account_validation"] = t.bank.validation

    for key, value in t.context.items():
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


def _normalize_conditions(rule: StripeRule) -> list[NormalizedCondition]:
    conditions: list[NormalizedCondition] = []

    top_level_fields = [
        ("account_country", rule.account_country),
        ("payment_method", rule.payment_method),
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
        ("currency_conversion_required", rule.currency_conversion_required),
        ("recurring", rule.recurring),
        ("billing_type", rule.billing_type),
        ("pricing_plan", rule.pricing_plan),
        ("pricing_tier", rule.pricing_tier),
        ("product_feature", rule.product_feature),
        ("integration_type", rule.integration_type),
        ("contract_length", rule.contract_length),
        ("dispute_state", rule.dispute_state),
        ("settlement_timing", rule.settlement_timing),
        ("transaction_region", rule.transaction_region),
        ("transaction_type", rule.transaction_type),
        ("cross_border", rule.cross_border),
        ("feature_enabled", rule.feature_enabled),
        ("payer", rule.payer),
        ("success", rule.success),
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
        conditions.append(NormalizedCondition(condition.dimension, str(condition.operator).lower(), condition.value))

    return conditions


def _condition_matches(condition: NormalizedCondition, context: dict[str, Any]) -> bool:
    actual = context.get(condition.dimension)
    expected = condition.value
    operator = condition.operator

    if operator in {"eq", "==", "equals"}:
        if actual is None and expected is not None:
            return False
        return _values_equal(actual, expected)
    if operator in {"ne", "!=", "not_equals"}:
        if actual is None:
            return False
        return not _values_equal(actual, expected)
    if operator == "in":
        if actual is None:
            return False
        return any(_values_equal(actual, item) for item in _as_list(expected))
    if operator in {"not_in", "nin"}:
        if actual is None:
            return False
        return all(not _values_equal(actual, item) for item in _as_list(expected))
    if operator in {"gt", "gte", "lt", "lte"}:
        return _numeric_compare(actual, expected, operator)
    raise UnsupportedFeeShape(
        f"Unsupported condition operator: {operator}",
        dimension=condition.dimension,
        operator=operator,
    )


def _condition_status(condition: NormalizedCondition, context: dict[str, Any]) -> str:
    actual = context.get(condition.dimension)
    expected = condition.value
    operator = condition.operator

    if actual is None and expected is not None:
        return "missing"

    if operator in {"eq", "==", "equals"}:
        return "match" if _values_equal(actual, expected) else "conflict"
    if operator in {"ne", "!=", "not_equals"}:
        return "match" if not _values_equal(actual, expected) else "conflict"
    if operator == "in":
        return "match" if any(_values_equal(actual, item) for item in _as_list(expected)) else "conflict"
    if operator in {"not_in", "nin"}:
        return "match" if all(not _values_equal(actual, item) for item in _as_list(expected)) else "conflict"
    if operator in {"gt", "gte", "lt", "lte"}:
        return "match" if _numeric_compare(actual, expected, operator) else "conflict"
    raise UnsupportedFeeShape(
        f"Unsupported condition operator: {operator}",
        dimension=condition.dimension,
        operator=operator,
    )


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) is bool(right)
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, (int, float, Decimal)) or isinstance(right, (int, float, Decimal)):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except Exception:
            return False
    return left == right


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _numeric_compare(actual: Any, expected: Any, operator: str) -> bool:
    try:
        left = Decimal(str(actual))
        right = Decimal(str(expected))
    except Exception as exc:
        raise UnsupportedFeeShape(
            "Numeric condition contains a non-numeric value.",
            actual=actual,
            expected=expected,
        ) from exc
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[operator]


def _specificity(rule: StripeRule) -> int:
    return len(_normalize_conditions(rule))


def _rule_financial_signature(rule: StripeRule, currency: str) -> tuple[Any, ...]:
    compiled = _compile_stripe_components(rule, currency)
    return (
        compiled.get("percentage"),
        compiled.get("fixed_amount"),
        compiled.get("minimum_amount"),
        compiled.get("maximum_amount"),
        compiled.get("behavior"),
    )


def _missing_fields(rule: StripeRule, context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for condition in _normalize_conditions(rule):
        actual = context.get(condition.dimension)
        if actual is None and condition.dimension not in {"transaction_amount"}:
            missing.append(_api_field_name(condition.dimension))
    return missing


def _api_field_name(dimension: str) -> str:
    mapping = {
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
        "customer_country": "customer_country",
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
        "success": "transaction.context.success",
        "bank_account_validation": "transaction.bank.validation",
        "fee_type": "transaction.context.fee_type",
    }
    return mapping.get(dimension, f"transaction.context.{dimension}")


def _is_calculable_status(status: str) -> bool:
    return status in {"calculable_rule", "free", "included"}


def _compile_stripe_components(rule: StripeRule, currency: str) -> dict[str, Any]:
    base_percentage = Decimal("0")
    base_fixed = Decimal("0")
    additive_percentage = Decimal("0")
    additive_fixed = Decimal("0")
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    unsupported: list[str] = []

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

    for comp in components:
        if comp.type in {"percentage", "percentage_surcharge"}:
            rate = _component_rate(comp)
            if comp.type == "percentage_surcharge":
                additive_percentage += rate
            else:
                base_percentage += rate
        elif comp.type in {"fixed_amount", "fixed_surcharge"}:
            amount = _component_fixed(comp, currency, rule.rule_id)
            if comp.type == "fixed_surcharge":
                additive_fixed += amount
            else:
                base_fixed += amount
        elif comp.type == "minimum_fee":
            minimum_amount = _component_fixed(comp, currency, rule.rule_id)
        elif comp.type == "maximum_fee":
            maximum_amount = _component_fixed(comp, currency, rule.rule_id)
        else:
            unsupported.append(comp.type)

    if unsupported:
        raise UnsupportedFeeShape(
            "Unsupported Stripe fee component type.",
            rule_id=rule.rule_id,
            types=unsupported,
        )

    behavior = "included" if rule.classification_status in {"free", "included"} else rule.behavior
    if behavior == "additive":
        percentage = additive_percentage or None
        fixed_amount = additive_fixed or None
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


def _component_rate(comp: Any) -> Decimal:
    if comp.basis_points is not None:
        return to_decimal(comp.basis_points, "basis points") / Decimal("100")
    if comp.value is not None:
        return to_decimal(comp.value, "percentage")
    raise UnsupportedFeeShape(
        "Percentage component missing basis_points and value.",
        component=comp.type,
    )


def _component_fixed(comp: Any, currency: str, rule_id: str) -> Decimal:
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


def _executable_from_rule(rule: StripeRule, currency: str) -> ExecutableFeeRule:
    compiled = _compile_stripe_components(rule, currency)
    label = rule.label or rule.name or rule.rule_id
    component_type = "processing"
    if compiled["behavior"] == "additive":
        component_type = "surcharge"
    elif compiled["behavior"] in {"free", "included"}:
        component_type = "included"
    return ExecutableFeeRule(
        rule_id=rule.rule_id,
        label=label,
        component_type=component_type,
        behavior=compiled["behavior"],
        percentage=compiled.get("percentage"),
        fixed_amount=compiled.get("fixed_amount"),
        fixed_currency=currency,
        minimum_amount=compiled.get("minimum_amount"),
        maximum_amount=compiled.get("maximum_amount"),
        currency=currency,
        classification_status=rule.classification_status,
        confidence=rule.confidence,
        exactness=rule.exactness,
        source_url=rule.source_url,
        metadata={
            "product_id": rule.product_id,
            "variant_id": rule.variant_id,
        },
    )


class StripeProvider:
    provider_id = "stripe"

    def __init__(
        self,
        core: StripeCoreFees,
        index: StripeIndex | None = None,
        payment_methods: StripePaymentMethods | None = None,
        data_ref: str | None = None,
    ) -> None:
        self.core = core
        self.index = index
        self.payment_methods = payment_methods
        self.data_ref = data_ref
        self._markets = {m.account_country.upper(): m for m in core.markets}
        self._index_map: dict[str, StripeIndexMarket] = {}
        if index:
            self._index_map = {im.account_country.upper(): im for im in index.markets}

    @classmethod
    def from_paths(
        cls,
        path: str,
        data_ref: str | None = None,
        validate_schema: bool = False,
    ) -> StripeProvider:
        core_path = load_json(f"{path}/json/core-fees.json")
        index_path = load_json(f"{path}/json/index.json")
        payment_methods_path = None
        with contextlib.suppress(Exception):
            payment_methods_path = load_json(f"{path}/json/payment-methods.json")
        if validate_schema:
            from payment_fee.data import validate_json_schema

            validate_json_schema(core_path, f"{path}/schemas/core-fees-v1.schema.json", "stripe-core")
            validate_json_schema(index_path, f"{path}/schemas/index-v1.schema.json", "stripe-index")
            if payment_methods_path:
                validate_json_schema(
                    payment_methods_path,
                    f"{path}/schemas/payment-methods-v1.schema.json",
                    "stripe-payment-methods",
                )
        core = StripeCoreFees.model_validate(core_path)
        index = StripeIndex.model_validate(index_path)
        payment_methods = StripePaymentMethods.model_validate(payment_methods_path) if payment_methods_path else None
        if core.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedFeeShape(
                f"Unsupported Stripe schema version: {core.schema_version}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return cls(
            core=core,
            index=index,
            payment_methods=payment_methods,
            data_ref=data_ref,
        )

    @classmethod
    def from_documents(
        cls,
        core: dict[str, Any],
        index: dict[str, Any] | None = None,
        payment_methods: dict[str, Any] | None = None,
        data_ref: str | None = None,
    ) -> StripeProvider:
        core_model = StripeCoreFees.model_validate(core)
        index_model = StripeIndex.model_validate(index) if index else None
        payment_methods_model = StripePaymentMethods.model_validate(payment_methods) if payment_methods else None
        if core_model.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedFeeShape(
                f"Unsupported Stripe schema version: {core_model.schema_version}",
                supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
        return cls(
            core=core_model,
            index=index_model,
            payment_methods=payment_methods_model,
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
        market = self._market(request.account_country)
        context = _build_stripe_context(request)
        currency = request.amount.currency

        rule_evaluations: list[tuple[StripeRule, list[str], bool]] = []
        for rule in market.rules:
            if not _is_calculable_status(rule.classification_status):
                continue
            try:
                statuses: list[str] = []
                for condition in _normalize_conditions(rule):
                    statuses.append(_condition_status(condition, context))
                if "conflict" in statuses:
                    continue
                missing = sorted(
                    {
                        _api_field_name(condition.dimension)
                        for condition, status in zip(_normalize_conditions(rule), statuses, strict=False)
                        if status == "missing"
                    }
                )
                rule_evaluations.append((rule, missing, not bool(missing)))
            except UnsupportedFeeShape:
                continue

        full_matches = [(r, m) for r, m, full in rule_evaluations if full]
        missing_evaluations = [(r, m) for r, m, full in rule_evaluations if not full and m]

        if missing_evaluations:
            max_full_spec = max((0,)) if not full_matches else max(_specificity(r) for r, _ in full_matches)
            blocker_missing: list[str] = []
            for rule, missing in missing_evaluations:
                if _specificity(rule) > max_full_spec:
                    blocker_missing.extend(missing)
            if blocker_missing:
                raise InsufficientTransactionContext(
                    sorted(set(blocker_missing)),
                    provider=self.provider_id,
                    market=request.account_country,
                    candidate_rule_ids=[r.rule_id for r, _ in missing_evaluations if _specificity(r) > max_full_spec],
                )

        if not full_matches:
            if missing_evaluations:
                all_missing = sorted({m for _, missing in missing_evaluations for m in missing})
                raise InsufficientTransactionContext(
                    all_missing,
                    provider=self.provider_id,
                    market=request.account_country,
                )
            raise QuoteNotAvailable(
                "No Stripe fee rule matched the supplied context.",
                provider=self.provider_id,
                market=request.account_country,
            )

        max_spec = max(_specificity(r) for r, _ in full_matches)
        most_specific = [r for r, _ in full_matches if _specificity(r) == max_spec]

        if len(most_specific) > 1:
            signatures = {_rule_financial_signature(r, currency) for r in most_specific}
            if len(signatures) > 1:
                raise AmbiguousFeeRules(
                    [r.rule_id for r in most_specific],
                    provider=self.provider_id,
                    market=request.account_country,
                )

        base_rules = [r for r in most_specific if r.behavior != "additive"]
        additive_rules = [r for r in market.rules if r.behavior == "additive"]

        if not base_rules:
            base_rules = most_specific[:1]

        selected_base = sorted(base_rules, key=lambda r: r.rule_id)[0]
        additive_selected = [
            r
            for r in additive_rules
            if _rule_matches_additive(r, context) and _is_calculable_status(r.classification_status)
        ]

        rules = [_executable_from_rule(selected_base, currency)]
        for r in additive_selected:
            try:
                rules.append(_executable_from_rule(r, currency))
            except (QuoteNotAvailable, UnsupportedFeeShape):
                continue

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
        products: set[str] = set()
        variants: set[str] = set()
        payment_methods: set[str] = set()
        fee_shapes: set[str] = set()
        currencies: set[str] = set()
        dimensions: set[str] = set()
        allowed: dict[str, set[Any]] = {}
        calculable: dict[str, set[str]] = {}

        for rule in market.rules:
            if not _is_calculable_status(rule.classification_status):
                continue
            if rule.product_id:
                products.add(rule.product_id)
                if rule.variant_id:
                    variants.add(rule.variant_id)
                    calculable.setdefault(rule.product_id, set()).add(rule.variant_id)
            if rule.payment_method:
                payment_methods.add(rule.payment_method)
            for comp in rule.fee_components:
                fee_shapes.add(comp.type)
            for condition in _normalize_conditions(rule):
                dimensions.add(condition.dimension)
                if condition.dimension not in allowed:
                    allowed[condition.dimension] = set()
                if isinstance(condition.value, list):
                    allowed[condition.dimension].update(str(v) for v in condition.value)
                else:
                    allowed[condition.dimension].add(str(condition.value))

        index_entry = self._index_map.get(account_country.upper())
        return CapabilityInfo(
            provider=self.provider_id,
            account_country=account_country.upper(),
            quotable=bool(calculable),
            product_ids=sorted(products),
            variants=sorted(variants),
            payment_methods=sorted(payment_methods),
            supported_fee_shapes=sorted(fee_shapes),
            supported_currencies=sorted(currencies),
            condition_dimensions=sorted(dimensions),
            allowed_values={k: sorted(v) for k, v in allowed.items()},
            required_context=sorted(_required_context(market)),
            calculable_products={k: sorted(v) for k, v in calculable.items()},
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


def _rule_matches_additive(rule: StripeRule, context: dict[str, Any]) -> bool:
    if rule.behavior != "additive":
        return False
    for condition in _normalize_conditions(rule):
        if condition.dimension == "payment_method" and context.get("payment_method") is None:
            continue
        if _condition_status(condition, context) != "match":
            return False
    return True


def _required_context(market: Any) -> set[str]:
    required: set[str] = set()
    for rule in market.rules:
        if not _is_calculable_status(rule.classification_status):
            continue
        for condition in _normalize_conditions(rule):
            if condition.dimension == "account_country":
                continue
            actual = getattr(rule, condition.dimension, None)
            if actual is not None:
                required.add(_api_field_name(condition.dimension))
    return required


def _stripe_request_schema(cap: CapabilityInfo) -> dict[str, Any]:
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
                    "product_id": {"enum": cap.product_ids} if cap.product_ids else {"type": "string"},
                    "variant_id": {"enum": cap.variants} if cap.variants else {"type": "string"},
                    "payment_method": {"enum": cap.payment_methods} if cap.payment_methods else {"type": "string"},
                    "channel": {"enum": ["online", "in_person"]},
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
                        "additionalProperties": {"type": ["string", "number", "boolean", "null", "array"]},
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["provider", "amount", "account_country", "transaction"],
    }
