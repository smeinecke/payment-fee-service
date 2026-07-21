from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, cast

from payment_fee.errors import CurrencyMismatch, QuoteNotAvailable
from payment_fee.models import (
    DataProvenance,
    FeeComponent,
    MatchedRule,
    Money,
    QuoteResponse,
)
from payment_fee.rules import CompiledFeePlan, ExecutableFeeRule

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

_CURRENCY_QUANTA: dict[str, Decimal] = {
    **{c: Decimal("1") for c in ZERO_DECIMAL_CURRENCIES},
    **{c: Decimal("0.001") for c in THREE_DECIMAL_CURRENCIES},
}
_DEFAULT_QUANTUM = Decimal("0.01")


def currency_quantum(currency: str) -> Decimal:
    return _CURRENCY_QUANTA.get(currency, _DEFAULT_QUANTUM)


def quantize_money(value: Decimal, currency: str) -> Decimal:
    return value.quantize(currency_quantum(currency), rounding=ROUND_HALF_UP)


def to_decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise QuoteNotAvailable(
            f"Invalid numeric value for {field}.",
            field=field,
            value=value,
        ) from exc


class FeeCalculator:
    def calculate(self, amount: Money, plan: CompiledFeePlan) -> QuoteResponse:
        if plan.currency and amount.currency != plan.currency:
            raise CurrencyMismatch(
                "Transaction currency does not match the fee plan currency.",
                transaction_currency=amount.currency,
                plan_currency=plan.currency,
            )

        components: list[FeeComponent] = []
        matched_rules: list[MatchedRule] = []

        for rule in plan.rules:
            if rule.behavior in ("free", "included", "waived"):
                components.append(
                    FeeComponent(
                        type=rule.component_type or "included",
                        label=rule.label,
                        amount=Decimal("0"),
                        currency=amount.currency,
                        source_rule_id=rule.rule_id,
                    )
                )
                matched_rules.append(self._matched_rule(rule))
                continue

            component = self._calculate_rule(amount, rule)
            if component.amount != 0 or rule.behavior == "base":
                components.append(component)
            matched_rules.append(self._matched_rule(rule))

        if not components:
            raise QuoteNotAvailable(
                "No calculable fee components were produced.",
                provider=plan.provider,
                market=plan.market,
            )

        total = quantize_money(
            sum((component.amount for component in components), Decimal("0")),
            amount.currency,
        )
        net = quantize_money(amount.value - total, amount.currency)
        status = self._derive_status(plan, components)

        return QuoteResponse(
            provider=plan.provider,
            status=status,
            amount=Money(
                value=quantize_money(amount.value, amount.currency),
                currency=amount.currency,
            ),
            processing_fee=Money(value=total, currency=amount.currency),
            net_amount=Money(value=net, currency=amount.currency),
            components=components,
            matched_rules=matched_rules,
            selected_product_id=plan.product_id,
            selected_variant_id=plan.variant_id,
            assumptions=plan.assumptions,
            warnings=plan.warnings,
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
        if rule.fixed_amount is not None and rule.fixed_currency not in (
            None,
            amount.currency,
        ):
            raise CurrencyMismatch(
                "A selected fee rule uses a fixed amount in a different currency.",
                rule_id=rule.rule_id,
                fixed_currency=rule.fixed_currency,
                transaction_currency=amount.currency,
            )

        raw = Decimal("0")
        rate_percentage: Decimal | None = None

        if rule.basis_points is not None:
            rate_percentage = rule.basis_points / Decimal("100")
            raw += amount.value * rule.basis_points / Decimal("10000")
        elif rule.percentage is not None:
            rate_percentage = rule.percentage
            raw += amount.value * rule.percentage / Decimal("100")

        if rule.fixed_amount is not None:
            raw += rule.fixed_amount

        minimum_applied = False
        maximum_applied = False

        if rule.minimum_amount is not None and raw < rule.minimum_amount:
            raw = rule.minimum_amount
            minimum_applied = True

        if rule.maximum_amount is not None and raw > rule.maximum_amount:
            raw = rule.maximum_amount
            maximum_applied = True

        calculated = quantize_money(raw, amount.currency)

        return FeeComponent(
            type=rule.component_type or "processing",
            label=rule.label,
            amount=calculated,
            currency=amount.currency,
            rate_percentage=rate_percentage,
            fixed_amount=quantize_money(rule.fixed_amount, rule.fixed_currency or amount.currency)
            if rule.fixed_amount is not None
            else None,
            minimum_applied=minimum_applied,
            maximum_applied=maximum_applied,
            payer=rule.payer,
            unit=rule.unit,
            source_rule_id=rule.rule_id,
        )

    @staticmethod
    def _matched_rule(rule: ExecutableFeeRule) -> MatchedRule:
        return MatchedRule(
            rule_id=rule.rule_id,
            classification_status=rule.classification_status,
            confidence=rule.confidence,
            exactness=rule.exactness,
            source_url=rule.source_url,
        )

    @staticmethod
    def _derive_status(
        plan: CompiledFeePlan, components: list[FeeComponent]
    ) -> Literal["exact_for_public_rate", "estimated", "range", "included"]:
        if all(rule.behavior in ("free", "included", "waived") for rule in plan.rules):
            return "included"

        for rule in plan.rules:
            if rule.exactness in ("range", "from", "up_to"):
                return cast(
                    Literal["exact_for_public_rate", "estimated", "range", "included"],
                    "range",
                )

        estimated = False
        for rule in plan.rules:
            if rule.exactness and rule.exactness not in ("exact", "exact_for_public_rate"):
                estimated = True
            if rule.classification_status and rule.classification_status not in (
                "calculable",
                "calculable_rule",
                "exact",
                "exact_for_public_rate",
            ):
                estimated = True

        return cast(
            Literal["exact_for_public_rate", "estimated", "range", "included"],
            "estimated" if estimated else "exact_for_public_rate",
        )
