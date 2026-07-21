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
    ProviderDataUnavailable,
    QuoteNotAvailable,
    UnknownMarket,
    UnsupportedFeeShape,
)
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema, StripeQuoteRequest
from payment_fee.providers.base import _check_schema_version
from payment_fee.providers.stripe.models import (
    StripeCoreFees,
    StripeFeeComponent,
    StripeIndex,
    StripeIndexMarket,
    StripePaymentMethods,
    StripeRule,
)
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule
from payment_fee.util import _as_list

SUPPORTED_SCHEMA_VERSIONS = {1}

SUPPORTED_COMPONENT_TYPES = {
    "fixed_amount",
    "percentage",
    "percentage_surcharge",
    "fixed_surcharge",
    "minimum_fee",
    "maximum_fee",
}

EVALUABLE_CLASSIFICATION_STATUSES = {"calculable_rule", "free", "included"}


@dataclass
class NormalizedCondition:
    dimension: str
    operator: str
    value: Any


SUPPORTED_OPERATORS = {"eq", "==", "equals", "ne", "!=", "not_equals", "in", "not_in", "nin", "gt", "gte", "lt", "lte"}


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

    for key, value in t.context.items():
        if key in context:
            if context[key] is None:
                context[key] = value
            elif value != context[key]:
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


def _condition_matches(condition: NormalizedCondition, context: dict[str, Any]) -> bool:
    actual = context.get(condition.dimension)
    expected = condition.value
    operator = condition.operator

    if actual is None and expected is not None:
        return False

    if operator in {"eq", "==", "equals"}:
        if isinstance(expected, list):
            return any(_values_equal(actual, item) for item in expected)
        return _values_equal(actual, expected)
    if operator in {"ne", "!=", "not_equals"}:
        if isinstance(expected, list):
            return all(not _values_equal(actual, item) for item in expected)
        return not _values_equal(actual, expected)
    if operator == "in":
        return any(_values_equal(actual, item) for item in _as_list(expected))
    if operator in {"not_in", "nin"}:
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
        if isinstance(expected, list):
            return "match" if any(_values_equal(actual, item) for item in expected) else "conflict"
        return "match" if _values_equal(actual, expected) else "conflict"
    if operator in {"ne", "!=", "not_equals"}:
        if isinstance(expected, list):
            return "match" if all(not _values_equal(actual, item) for item in expected) else "conflict"
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
        "bank_transfer_type": "transaction.bank.transfer_type",
        "fee_type": "transaction.context.fee_type",
        "transaction_amount": "amount.value",
    }
    return mapping.get(dimension, f"transaction.context.{dimension}")


def _is_evaluable(rule: StripeRule) -> bool:
    return rule.classification_status in EVALUABLE_CLASSIFICATION_STATUSES


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
        if comp.type == "percentage":
            base_percentage += _component_rate(comp)
        elif comp.type == "percentage_surcharge":
            additive_percentage += _component_rate(comp)
        elif comp.type == "fixed_amount":
            base_fixed += _component_fixed(comp, currency, rule.rule_id)
        elif comp.type == "fixed_surcharge":
            additive_fixed += _component_fixed(comp, currency, rule.rule_id)
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
        payer=rule.payer,
        unit=rule.unit,
        classification_status=rule.classification_status,
        confidence=rule.confidence,
        exactness=rule.exactness,
        source_url=rule.source_url,
        metadata={
            "product_id": rule.product_id,
            "variant_id": rule.variant_id,
        },
    )


def _sanitize_index_document(index: dict[str, Any] | None) -> dict[str, Any] | None:
    if not index:
        return index
    for market in index.get("markets", []):
        market.pop("schema_version", None)
    return index


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
        index_path = _sanitize_index_document(index_path)
        payment_methods_path = None
        with contextlib.suppress(Exception):
            payment_methods_path = load_json(f"{path}/json/payment-methods.json")
        if validate_schema:
            from payment_fee.data import validate_json_schema

            validate_json_schema(core_path, f"{path}/schemas/core-fees-v1.schema.json", "stripe-core")
            if index_path is None:
                raise ProviderDataUnavailable("stripe", "index.json is missing or empty")
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
        _check_schema_version(core, SUPPORTED_SCHEMA_VERSIONS, "Stripe")
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
            if payment_methods is not None:
                if schemas is None or "payment_methods" not in schemas:
                    raise DatasetValidationError(
                        "Stripe payment-methods schema is required for document validation.",
                        schema="payment_methods",
                    )
                validate_json_schema(payment_methods, schemas["payment_methods"], "stripe-payment-methods")
        core_model = StripeCoreFees.model_validate(core_document)
        index_model = StripeIndex.model_validate(index_document) if index_document else None
        payment_methods_model = StripePaymentMethods.model_validate(payment_methods) if payment_methods else None
        _check_schema_version(core_model, SUPPORTED_SCHEMA_VERSIONS, "Stripe")
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

        full_matches: list[tuple[StripeRule, int]] = []
        missing_matches: list[tuple[StripeRule, list[str], int]] = []

        for rule in market.rules:
            conditions = _normalize_conditions(rule)
            conflict = False
            missing: list[str] = []
            for condition in conditions:
                status = _condition_status(condition, context)
                if status == "conflict":
                    conflict = True
                    break
                if status == "missing":
                    missing.append(_api_field_name(condition.dimension))
            if conflict:
                continue
            specificity = len(conditions)
            if missing:
                missing_matches.append((rule, sorted(set(missing)), specificity))
            else:
                full_matches.append((rule, specificity))

        if not full_matches:
            if missing_matches:
                all_missing = sorted({m for _, missing, _ in missing_matches for m in missing})
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

        max_full_spec = max(spec for _, spec in full_matches)
        most_specific_full = [r for r, spec in full_matches if spec == max_full_spec]

        if not any(_is_evaluable(r) for r in most_specific_full):
            rule_statuses = {r.classification_status for r in most_specific_full}
            if "unsupported" in rule_statuses:
                raise UnsupportedFeeShape(
                    "The most specific matching Stripe fee rule is unsupported.",
                    provider=self.provider_id,
                    market=request.account_country,
                    rule_ids=[r.rule_id for r in most_specific_full],
                )
            raise QuoteNotAvailable(
                "The most specific matching Stripe fee rule cannot be quoted.",
                provider=self.provider_id,
                market=request.account_country,
                rule_ids=[r.rule_id for r in most_specific_full],
            )

        if missing_matches:
            more_specific_missing = [
                (rule, missing, spec)
                for rule, missing, spec in missing_matches
                if spec > max_full_spec and _is_evaluable(rule)
            ]
            if more_specific_missing:
                blocker_missing = sorted({m for _, missing, _ in more_specific_missing for m in missing})
                raise InsufficientTransactionContext(
                    blocker_missing,
                    provider=self.provider_id,
                    market=request.account_country,
                    candidate_rule_ids=[r.rule_id for r, _, _ in more_specific_missing],
                )

        base_candidates: list[StripeRule] = []
        for spec in sorted({spec for _, spec in full_matches}, reverse=True):
            candidates = [r for r, s in full_matches if s == spec and _is_evaluable(r) and r.behavior != "additive"]
            if candidates:
                base_candidates = candidates
                break

        if not base_candidates:
            raise QuoteNotAvailable(
                "No evaluable base Stripe fee rule matched the supplied context.",
                provider=self.provider_id,
                market=request.account_country,
            )

        if len(base_candidates) > 1:
            signatures = {_rule_financial_signature(r, currency) for r in base_candidates}
            if len(signatures) > 1:
                raise AmbiguousFeeRules(
                    [r.rule_id for r in base_candidates],
                    provider=self.provider_id,
                    market=request.account_country,
                )

        selected_base = sorted(base_candidates, key=lambda r: r.rule_id)[0]

        additive_rules = self._select_additive_rules(market.rules, context, request.account_country)

        rules = [_executable_from_rule(selected_base, currency)]
        for rule in additive_rules:
            rules.append(_executable_from_rule(rule, currency))

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
        rules: list[StripeRule],
        context: dict[str, Any],
        account_country: str,
    ) -> list[StripeRule]:
        selected: list[StripeRule] = []
        for rule in rules:
            if rule.behavior != "additive":
                continue
            conditions = _normalize_conditions(rule)
            statuses: list[str] = []
            payment_method_missing = False
            conflict = False
            for condition in conditions:
                status = _condition_status(condition, context)
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

        for rule in market.rules:
            product_id = rule.product_id
            variant_id = rule.variant_id
            if product_id:
                products.add(product_id)
            if variant_id:
                variants.add(variant_id)
            if rule.payment_method:
                payment_methods.add(rule.payment_method)
            for comp in rule.fee_components:
                fee_shapes.add(comp.type)
                if comp.currency:
                    currencies.add(comp.currency.upper())
            if rule.fixed_currency:
                currencies.add(rule.fixed_currency.upper())

            bucket = _classify_rule(rule)
            target = {
                "calculable": calculable,
                "included": included,
                "custom_pricing": custom_pricing,
                "unsupported": unsupported,
                "non_calculable": non_calculable,
            }.get(bucket)
            if target is not None and product_id and variant_id:
                target.setdefault(product_id, set()).add(variant_id)

            for condition in _normalize_conditions(rule):
                dimensions.add(condition.dimension)
                if condition.dimension not in allowed:
                    allowed[condition.dimension] = set()
                if isinstance(condition.value, list):
                    for item in condition.value:
                        if item is not None:
                            allowed[condition.dimension].add(str(item))
                elif condition.value is not None:
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
            included_products={k: sorted(v) for k, v in included.items()},
            custom_pricing_products={k: sorted(v) for k, v in custom_pricing.items()},
            unsupported_products={k: sorted(v) for k, v in unsupported.items()},
            non_calculable_products={k: sorted(v) for k, v in non_calculable.items()},
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
