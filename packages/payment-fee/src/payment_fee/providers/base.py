from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from payment_fee.errors import QuoteNotAvailable, UnsupportedFeeShape
from payment_fee.models import BaseQuoteRequest, CapabilityInfo, MarketInfo, QuoteSchema
from payment_fee.rules import CompiledFeePlan
from payment_fee.util import _as_list

SUPPORTED_SCHEMA_VERSIONS: set[int] = {1}


def _check_schema_version(model: Any, supported: set[int], provider_name: str) -> None:
    if model.schema_version not in supported:
        raise UnsupportedFeeShape(
            f"Unsupported {provider_name} schema version: {model.schema_version}",
            supported=sorted(supported),
        )


def _merge_context_overrides(context: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Merge free-form transaction context into a typed context, raising on contradiction."""
    for key, value in overrides.items():
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


@dataclass
class NormalizedCondition:
    dimension: str
    operator: str
    value: Any


SUPPORTED_OPERATORS: set[str] = {
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


def _api_field_name_lookup(mapping: dict[str, str], dimension: str) -> str:
    """Return the API field path for a dimension, falling back to transaction.context."""
    return mapping.get(dimension, f"transaction.context.{dimension}")


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


def _evaluate_condition(
    condition: NormalizedCondition, context: dict[str, Any]
) -> Literal["match", "conflict", "missing"]:
    actual = context.get(condition.dimension)
    expected = condition.value
    operator = condition.operator

    if actual is None and expected is not None:
        return "missing"

    if operator in {"eq", "==", "equals"}:
        if isinstance(expected, list):
            matched = any(_values_equal(actual, item) for item in expected)
        else:
            matched = _values_equal(actual, expected)
    elif operator in {"ne", "!=", "not_equals"}:
        if isinstance(expected, list):
            matched = all(not _values_equal(actual, item) for item in expected)
        else:
            matched = not _values_equal(actual, expected)
    elif operator == "in":
        matched = any(_values_equal(actual, item) for item in _as_list(expected))
    elif operator in {"not_in", "nin"}:
        matched = all(not _values_equal(actual, item) for item in _as_list(expected))
    elif operator in {"gt", "gte", "lt", "lte"}:
        matched = _numeric_compare(actual, expected, operator)
    else:
        raise UnsupportedFeeShape(
            f"Unsupported condition operator: {operator}",
            dimension=condition.dimension,
            operator=operator,
        )
    return "match" if matched else "conflict"


def _condition_matches(conditions: list[NormalizedCondition], context: dict[str, Any]) -> bool:
    """Return True when every condition evaluates to a match."""
    return all(_evaluate_condition(condition, context) == "match" for condition in conditions)


def _missing_dimensions(
    conditions: list[NormalizedCondition],
    context: dict[str, Any],
    api_field_name: Callable[[str], str],
) -> list[str]:
    """Collect API field paths for any condition that is missing in the context."""
    missing: list[str] = []
    for condition in conditions:
        if _evaluate_condition(condition, context) == "missing":
            missing.append(api_field_name(condition.dimension))
    return missing


class CapabilityAccumulator:
    """Collect provider capabilities shared across Stripe and PayPal.

    Each provider feeds a rule at a time; the accumulator tracks the seven
    common collections plus classification buckets.
    """

    _BUCKET_NAMES = ("calculable", "included", "custom_pricing", "unsupported", "non_calculable")

    def __init__(self, provider_id: str, account_country: str) -> None:
        self.provider_id = provider_id
        self.account_country = account_country
        self.products: set[str] = set()
        self.variants: set[str] = set()
        self.payment_methods: set[str] = set()
        self.fee_shapes: set[str] = set()
        self.currencies: set[str] = set()
        self.dimensions: set[str] = set()
        self.allowed: dict[str, set[Any]] = {}
        self.buckets: dict[str, dict[str, set[str]]] = {name: {} for name in self._BUCKET_NAMES}

    def add_rule(
        self,
        product_id: str | None,
        variant_id: str | None,
        payment_methods: Iterable[str] | str | None,
        fee_shapes: Iterable[str],
        currencies: Iterable[str],
        conditions: list[NormalizedCondition],
        classification_bucket: str,
        bucket_variant_id: str | None = None,
    ) -> None:
        if product_id:
            self.products.add(product_id)
        if variant_id:
            self.variants.add(variant_id)
        if payment_methods:
            if isinstance(payment_methods, str):
                self.payment_methods.add(payment_methods)
            else:
                self.payment_methods.update(str(pm) for pm in payment_methods if pm is not None)

        self.fee_shapes.update(fee_shapes)
        self.currencies.update(c.upper() for c in currencies if c)

        bucket = self.buckets.get(classification_bucket)
        bucket_variant = bucket_variant_id or variant_id
        if bucket is not None and product_id and bucket_variant:
            bucket.setdefault(product_id, set()).add(bucket_variant)

        for condition in conditions:
            self.dimensions.add(condition.dimension)
            allowed_set = self.allowed.setdefault(condition.dimension, set())
            value = condition.value
            if isinstance(value, list):
                for item in value:
                    if item is not None:
                        allowed_set.add(item if isinstance(item, bool) else str(item))
            elif value is not None:
                allowed_set.add(value if isinstance(value, bool) else str(value))

    def add_currencies(self, currencies: Iterable[str]) -> None:
        self.currencies.update(c.upper() for c in currencies if c)

    def to_capability_info(
        self,
        *,
        quotable: bool,
        required_context: list[str],
        dataset_status: str,
        source_revision: str | None,
    ) -> CapabilityInfo:
        return CapabilityInfo(
            provider=self.provider_id,
            account_country=self.account_country.upper(),
            quotable=quotable,
            product_ids=sorted(self.products),
            variants=sorted(self.variants),
            payment_methods=sorted(self.payment_methods),
            supported_fee_shapes=sorted(self.fee_shapes),
            supported_currencies=sorted(self.currencies),
            condition_dimensions=sorted(self.dimensions),
            allowed_values={k: sorted(v) for k, v in self.allowed.items()},
            required_context=sorted(required_context),
            calculable_products={k: sorted(v) for k, v in self.buckets["calculable"].items()},
            included_products={k: sorted(v) for k, v in self.buckets["included"].items()},
            custom_pricing_products={k: sorted(v) for k, v in self.buckets["custom_pricing"].items()},
            unsupported_products={k: sorted(v) for k, v in self.buckets["unsupported"].items()},
            non_calculable_products={k: sorted(v) for k, v in self.buckets["non_calculable"].items()},
            dataset_status=dataset_status,
            source_revision=source_revision,
        )


class FeeProvider(Protocol):
    provider_id: str

    def compile_rules(self, request: BaseQuoteRequest) -> CompiledFeePlan: ...

    def markets(self) -> list[MarketInfo]: ...

    def capabilities(self, account_country: str) -> CapabilityInfo: ...

    def quote_schema(self, account_country: str) -> QuoteSchema: ...

    def data_status(self) -> dict[str, Any]: ...
