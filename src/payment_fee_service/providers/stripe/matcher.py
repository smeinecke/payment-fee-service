from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from payment_fee_service.domain.errors import QuoteUnavailableError

TOP_LEVEL_DIMENSIONS = {
    "account_country",
    "customer_country",
    "payment_method",
    "card_origin",
    "card_region",
    "card_tier",
    "channel",
    "recurring",
    "billing_type",
    "presentment_currency",
    "settlement_currency",
    "currency_conversion_required",
}


def build_context(request: Any) -> dict[str, Any]:
    card = request.payment.card
    context = {
        "account_country": request.account_country,
        "customer_country": request.customer_country,
        "payment_method": request.payment.method,
        "card_origin": card.origin if card else None,
        "card_region": card.region if card else None,
        "card_tier": card.tier if card else None,
        "channel": request.payment.channel,
        "recurring": request.payment.recurring,
        "billing_type": request.payment.billing_type,
        "presentment_currency": request.amount.currency,
        "settlement_currency": request.settlement_currency or request.amount.currency,
        "currency_conversion_required": request.payment.currency_conversion_required,
        "transaction_amount": request.amount.value,
    }
    context.update(request.payment.context)
    return context


def rule_matches_known_context(rule: dict[str, Any], context: dict[str, Any]) -> bool:
    for dimension in TOP_LEVEL_DIMENSIONS:
        expected = rule.get(dimension)
        if expected is None:
            continue
        actual = context.get(dimension)
        if actual is None or not values_equal(actual, expected):
            return False

    amount = Decimal(str(context["transaction_amount"]))
    if rule.get("transaction_amount_min") is not None and amount < Decimal(
        str(rule["transaction_amount_min"])
    ):
        return False
    if rule.get("transaction_amount_max") is not None and amount > Decimal(
        str(rule["transaction_amount_max"])
    ):
        return False

    return all(condition_matches(condition, context) for condition in rule.get("conditions", []))


def missing_dimensions(rule: dict[str, Any], context: dict[str, Any]) -> list[str]:
    missing = [
        dimension
        for dimension in TOP_LEVEL_DIMENSIONS
        if rule.get(dimension) is not None and context.get(dimension) is None
    ]
    for condition in rule.get("conditions", []):
        dimension = condition.get("dimension")
        if dimension and context.get(dimension) is None:
            missing.append(str(dimension))
    return missing


def condition_matches(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    dimension = str(condition.get("dimension"))
    actual = context.get(dimension)
    if actual is None:
        return False
    expected = condition.get("value")
    operator = str(condition.get("operator", "eq")).lower()

    if operator in {"eq", "==", "equals"}:
        return values_equal(actual, expected)
    if operator in {"ne", "!=", "not_equals"}:
        return not values_equal(actual, expected)
    if operator == "in":
        return any(values_equal(actual, item) for item in ensure_list(expected))
    if operator in {"not_in", "nin"}:
        return all(not values_equal(actual, item) for item in ensure_list(expected))
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            left, right = Decimal(str(actual)), Decimal(str(expected))
        except InvalidOperation as exc:
            raise QuoteUnavailableError(
                "A numeric Stripe fee condition contains a non-numeric value.",
                dimension=dimension,
                operator=operator,
            ) from exc
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    raise QuoteUnavailableError(
        "Unsupported Stripe fee-condition operator.",
        dimension=dimension,
        operator=operator,
    )


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return left == right


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def specificity(rule: dict[str, Any]) -> int:
    score = sum(1 for dimension in TOP_LEVEL_DIMENSIONS if rule.get(dimension) is not None)
    score += len(rule.get("conditions", []))
    score += int(rule.get("transaction_amount_min") is not None)
    score += int(rule.get("transaction_amount_max") is not None)
    return score


def financial_signature(rule: dict[str, Any]) -> tuple[Any, ...]:
    return (
        rule.get("basis_points"),
        rule.get("percentage"),
        rule.get("fixed_amount"),
        rule.get("fixed_currency"),
        rule.get("minimum_amount"),
        rule.get("maximum_amount"),
    )
