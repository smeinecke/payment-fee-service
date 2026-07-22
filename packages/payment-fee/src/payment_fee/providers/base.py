from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from payment_fee.errors import AmbiguousFeeRules, InsufficientTransactionContext, QuoteNotAvailable, UnsupportedFeeShape
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
            elif not _values_equal(value, context[key]):
                raise QuoteNotAvailable(
                    "Contradictory duplicate value in transaction context.",
                    field=key,
                    typed_value=context[key],
                    context_value=value,
                )
        else:
            context[key] = value


def _match_candidates(
    candidates: Sequence[tuple[Any, list[NormalizedCondition], int | float]],
    context: dict[str, Any],
    api_field_name: Callable[[str], str],
) -> tuple[list[tuple[Any, int | float]], list[tuple[Any, list[str], int | float]]]:
    """Bucket candidates into full matches and missing-context matches."""
    full_matches: list[tuple[Any, int | float]] = []
    missing_matches: list[tuple[Any, list[str], int | float]] = []
    for rule, conditions, specificity in candidates:
        conflict = False
        missing: list[str] = []
        for condition in conditions:
            status = _evaluate_condition(condition, context)
            if status == "conflict":
                conflict = True
                break
            if status == "missing":
                missing.append(api_field_name(condition.dimension))
        if conflict:
            continue
        if missing:
            missing_matches.append((rule, sorted(set(missing)), specificity))
        else:
            full_matches.append((rule, specificity))
    return full_matches, missing_matches


def _is_more_specific(spec: int | float, max_spec: int | float) -> bool:
    """Return True when ``spec`` is meaningfully greater than ``max_spec``."""
    return spec > max_spec and not math.isclose(spec, max_spec, rel_tol=0, abs_tol=1e-9)


def _handle_no_match(
    full_matches: list[tuple[Any, int | float]],
    missing_matches: list[tuple[Any, list[str], int | float]],
    account_country: str,
    provider_id: str,
    error_context: dict[str, Any] | None,
) -> None:
    """Raise when no rule fully matched, preferring missing-context over no-match."""
    if full_matches:
        return
    if missing_matches:
        all_missing = sorted({m for _, missing, _ in missing_matches for m in missing})
        raise InsufficientTransactionContext(
            all_missing,
            provider=provider_id,
            market=account_country,
            **(error_context or {}),
        )
    raise QuoteNotAvailable(
        "No fee rule matched the supplied context.",
        provider=provider_id,
        market=account_country,
        **(error_context or {}),
    )


def _collect_most_specific_full(
    full_matches: list[tuple[Any, int | float]],
) -> tuple[list[Any], int | float]:
    """Return the rule(s) tied for highest specificity among full matches."""
    max_full_spec = max(spec for _, spec in full_matches)
    most_specific = [rule for rule, spec in full_matches if math.isclose(spec, max_full_spec, rel_tol=0, abs_tol=1e-9)]
    return most_specific, max_full_spec


def _require_evaluable_most_specific(
    most_specific_full: list[Any],
    *,
    is_evaluable: Callable[[Any], bool],
    classification_status: Callable[[Any], str] | None,
    unsupported_statuses: set[str] | None,
    account_country: str,
    provider_id: str,
    rule_id: Callable[[Any], str],
    not_calculable_message: str,
    error_context: dict[str, Any] | None,
) -> None:
    """Raise when the most-specific full match cannot be quoted."""
    if any(is_evaluable(rule) for rule in most_specific_full):
        return
    ctx = error_context or {}
    if (
        classification_status
        and unsupported_statuses
        and any(classification_status(rule) in unsupported_statuses for rule in most_specific_full)
    ):
        raise UnsupportedFeeShape(
            "The most specific matching fee rule is unsupported.",
            provider=provider_id,
            market=account_country,
            rule_ids=[rule_id(rule) for rule in most_specific_full],
            **ctx,
        )
    raise QuoteNotAvailable(
        not_calculable_message,
        provider=provider_id,
        market=account_country,
        rule_ids=[rule_id(rule) for rule in most_specific_full],
        **ctx,
    )


def _check_no_more_specific_missing(
    missing_matches: list[tuple[Any, list[str], int | float]],
    max_full_spec: int | float,
    *,
    is_evaluable: Callable[[Any], bool],
    account_country: str,
    provider_id: str,
    rule_id: Callable[[Any], str],
    error_context: dict[str, Any] | None,
) -> None:
    """Raise when a more-specific missing-context rule would change the selection."""
    more_specific_missing = [
        (rule, missing, spec)
        for rule, missing, spec in missing_matches
        if _is_more_specific(spec, max_full_spec) and is_evaluable(rule)
    ]
    if not more_specific_missing:
        return
    ctx = error_context or {}
    blocker_missing = sorted({m for _, missing, _ in more_specific_missing for m in missing})
    raise InsufficientTransactionContext(
        blocker_missing,
        provider=provider_id,
        market=account_country,
        candidate_rule_ids=[rule_id(rule) for rule, _, _ in more_specific_missing],
        **ctx,
    )


def _resolve_ambiguity(
    full_matches: list[tuple[Any, int | float]],
    max_full_spec: int | float,
    *,
    is_evaluable: Callable[[Any], bool],
    select_filter: Callable[[Any], bool] | None,
    financial_signature: Callable[[Any], Any] | None,
    rule_id: Callable[[Any], str],
    sort_key: Callable[[Any], Any] | None,
    account_country: str,
    provider_id: str,
    no_selectable_message: str,
    error_context: dict[str, Any] | None,
) -> Any:
    """Pick a single selectable rule from the most-specific tied set."""
    select_filter = select_filter or is_evaluable
    selectable_specs = [spec for rule, spec in full_matches if select_filter(rule)]
    if not selectable_specs:
        raise QuoteNotAvailable(
            no_selectable_message,
            provider=provider_id,
            market=account_country,
            **(error_context or {}),
        )

    select_max_spec = max(selectable_specs)
    selectable = [
        rule
        for rule, spec in full_matches
        if math.isclose(spec, select_max_spec, rel_tol=0, abs_tol=1e-9) and select_filter(rule)
    ]

    if len(selectable) > 1:
        ctx = error_context or {}
        if financial_signature is None:
            raise AmbiguousFeeRules(
                [rule_id(rule) for rule in selectable],
                provider=provider_id,
                market=account_country,
                **ctx,
            )
        signatures = {financial_signature(rule) for rule in selectable}
        if len(signatures) > 1:
            raise AmbiguousFeeRules(
                [rule_id(rule) for rule in selectable],
                provider=provider_id,
                market=account_country,
                **ctx,
            )

    sort_key = sort_key or rule_id
    return sorted(selectable, key=sort_key)[0]


def _select_single_rule(
    full_matches: list[tuple[Any, int | float]],
    missing_matches: list[tuple[Any, list[str], int | float]],
    account_country: str,
    provider_id: str,
    *,
    api_field_name: Callable[[str], str],
    is_evaluable: Callable[[Any], bool],
    select_filter: Callable[[Any], bool] | None = None,
    financial_signature: Callable[[Any], Any] | None = None,
    rule_id: Callable[[Any], str] = lambda r: str(r),
    sort_key: Callable[[Any], Any] | None = None,
    classification_status: Callable[[Any], str] | None = None,
    unsupported_statuses: set[str] | None = None,
    check_more_specific_missing: bool = True,
    not_calculable_message: str = "The most specific matching fee rule cannot be quoted.",
    no_selectable_message: str = "No evaluable fee rule matched the supplied context.",
    error_context: dict[str, Any] | None = None,
) -> tuple[Any, int | float]:
    """Select the single most-specific, evaluable, unambiguous rule from full matches."""
    _handle_no_match(full_matches, missing_matches, account_country, provider_id, error_context)
    most_specific_full, max_full_spec = _collect_most_specific_full(full_matches)
    _require_evaluable_most_specific(
        most_specific_full,
        is_evaluable=is_evaluable,
        classification_status=classification_status,
        unsupported_statuses=unsupported_statuses,
        account_country=account_country,
        provider_id=provider_id,
        rule_id=rule_id,
        not_calculable_message=not_calculable_message,
        error_context=error_context,
    )
    if check_more_specific_missing:
        _check_no_more_specific_missing(
            missing_matches,
            max_full_spec,
            is_evaluable=is_evaluable,
            account_country=account_country,
            provider_id=provider_id,
            rule_id=rule_id,
            error_context=error_context,
        )
    selected = _resolve_ambiguity(
        full_matches,
        max_full_spec,
        is_evaluable=is_evaluable,
        select_filter=select_filter,
        financial_signature=financial_signature,
        rule_id=rule_id,
        sort_key=sort_key,
        account_country=account_country,
        provider_id=provider_id,
        no_selectable_message=no_selectable_message,
        error_context=error_context,
    )
    return selected, max_full_spec


def compile_generic(
    candidates: Sequence[tuple[Any, list[NormalizedCondition], int | float]],
    context: dict[str, Any],
    account_country: str,
    provider_id: str,
    *,
    api_field_name: Callable[[str], str],
    is_evaluable: Callable[[Any], bool],
    select_filter: Callable[[Any], bool] | None = None,
    financial_signature: Callable[[Any], Any] | None = None,
    rule_id: Callable[[Any], str] = lambda r: str(r),
    sort_key: Callable[[Any], Any] | None = None,
    classification_status: Callable[[Any], str] | None = None,
    unsupported_statuses: set[str] | None = None,
    check_more_specific_missing: bool = True,
    require_all_evaluable: bool = False,
    not_calculable_message: str = "The most specific matching fee rule cannot be quoted.",
    no_selectable_message: str = "No evaluable fee rule matched the supplied context.",
    error_context: dict[str, Any] | None = None,
) -> tuple[Any, int | float]:
    """Shared rule-matching and selection pipeline for Stripe and PayPal.

    Each provider supplies its candidates (rule, normalized conditions, specificity)
    and the hooks that differ between providers.
    """
    full_matches, missing_matches = _match_candidates(candidates, context, api_field_name)

    if require_all_evaluable:
        for rule, _ in full_matches:
            if not is_evaluable(rule):
                status = classification_status(rule) if classification_status else None
                raise QuoteNotAvailable(
                    "A selected fee rule is not calculable.",
                    provider=provider_id,
                    market=account_country,
                    rule_id=rule_id(rule),
                    status=status,
                    **(error_context or {}),
                )

    return _select_single_rule(
        full_matches,
        missing_matches,
        account_country,
        provider_id,
        api_field_name=api_field_name,
        is_evaluable=is_evaluable,
        select_filter=select_filter,
        financial_signature=financial_signature,
        rule_id=rule_id,
        sort_key=sort_key,
        classification_status=classification_status,
        unsupported_statuses=unsupported_statuses,
        check_more_specific_missing=check_more_specific_missing,
        not_calculable_message=not_calculable_message,
        no_selectable_message=no_selectable_message,
        error_context=error_context,
    )


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
    if isinstance(left, bool):
        return isinstance(right, bool) and left is right
    if isinstance(right, bool):
        return False
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, int | float | Decimal) or isinstance(right, int | float | Decimal):
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

    def _compile_single_rule_for_audit(self, rule: Any, context: Any) -> Any: ...
