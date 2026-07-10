from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from payment_fee_service.domain.errors import QuoteUnavailableError
from payment_fee_service.domain.models import (
    DataProvenance,
    FeeComponent,
    MatchedRule,
    Money,
    QuoteResponse,
)
from payment_fee_service.domain.rules import CompiledFeePlan, ExecutableFeeRule

# ISO 4217 currencies with no minor unit. Everything else defaults to two.
ZERO_DECIMAL_CURRENCIES = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}
THREE_DECIMAL_CURRENCIES = {"BHD", "JOD", "KWD", "OMR", "TND"}


def currency_quantum(currency: str) -> Decimal:
    if currency in ZERO_DECIMAL_CURRENCIES:
        return Decimal("1")
    if currency in THREE_DECIMAL_CURRENCIES:
        return Decimal("0.001")
    return Decimal("0.01")


def quantize_money(value: Decimal, currency: str) -> Decimal:
    return value.quantize(currency_quantum(currency), rounding=ROUND_HALF_UP)


class FeeCalculator:
    def calculate(self, amount: Money, plan: CompiledFeePlan) -> QuoteResponse:
        components: list[FeeComponent] = []
        matched_rules: list[MatchedRule] = []

        for rule in plan.rules:
            component = self._calculate_rule(amount, rule)
            if component.amount != 0:
                components.append(component)
            matched_rules.append(
                MatchedRule(
                    rule_id=rule.rule_id,
                    classification_status=rule.classification_status,
                    confidence=rule.confidence,
                    exactness=rule.exactness,
                    source_url=rule.source_url,
                )
            )

        if not components:
            raise QuoteUnavailableError("No non-zero processing fee components were produced.")

        total = quantize_money(
            sum((component.amount for component in components), Decimal("0")),
            amount.currency,
        )
        net = quantize_money(amount.value - total, amount.currency)
        estimated = bool(plan.assumptions) or any(
            rule.exactness not in (None, "exact")
            or (rule.confidence is not None and rule.confidence < 1)
            for rule in plan.rules
        )

        return QuoteResponse(
            provider=plan.provider,
            status="estimated" if estimated else "exact_for_public_rate",
            amount=Money(
                value=quantize_money(amount.value, amount.currency), currency=amount.currency
            ),
            processing_fee=Money(value=total, currency=amount.currency),
            net_amount=Money(value=net, currency=amount.currency),
            components=components,
            matched_rules=matched_rules,
            assumptions=plan.assumptions,
            data=DataProvenance(
                provider=plan.provider,
                schema_version=plan.schema_version,
                market=plan.market,
                content_sha256=plan.content_sha256,
                source_urls=plan.source_urls,
                source_updated_at=plan.source_updated_at,
                data_ref=plan.data_ref,
            ),
        )

    def _calculate_rule(self, amount: Money, rule: ExecutableFeeRule) -> FeeComponent:
        if rule.behavior not in {"additive", "base", "standard"}:
            raise QuoteUnavailableError(
                "Unsupported fee-rule behavior.",
                rule_id=rule.rule_id,
                behavior=rule.behavior,
            )
        if rule.fixed_amount is not None and rule.fixed_currency not in (None, amount.currency):
            raise QuoteUnavailableError(
                "The fixed fee currency does not match the transaction currency.",
                rule_id=rule.rule_id,
                fixed_currency=rule.fixed_currency,
                transaction_currency=amount.currency,
            )

        rate_percentage: Decimal | None = None
        raw = Decimal("0")
        if rule.basis_points is not None:
            rate_percentage = rule.basis_points / Decimal("100")
            raw += amount.value * rule.basis_points / Decimal("10000")
        elif rule.percentage is not None:
            rate_percentage = rule.percentage
            raw += amount.value * rule.percentage / Decimal("100")

        if rule.fixed_amount is not None:
            raw += rule.fixed_amount
        if rule.minimum_amount is not None:
            raw = max(raw, rule.minimum_amount)
        if rule.maximum_amount is not None:
            raw = min(raw, rule.maximum_amount)

        calculated = quantize_money(raw, amount.currency)
        return FeeComponent(
            type=rule.component_type,
            label=rule.label,
            amount=calculated,
            currency=amount.currency,
            rate_percentage=rate_percentage,
            fixed_amount=rule.fixed_amount,
            source_rule_id=rule.rule_id,
        )
