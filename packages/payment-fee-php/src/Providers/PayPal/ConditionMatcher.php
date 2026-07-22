<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\PayPal;

use Brick\Math\BigDecimal;
use Smeinecke\PaymentFee\Exception\UnsupportedFeeShape;

/**
 * Condition-matching engine for PayPal fee rules.
 *
 * Mirrors the shared condition-matcher logic used by the Python reference
 * implementation and keeps the provider focused on request handling and
 * rule selection.
 */
final class ConditionMatcher
{
    /**
     * @param array<string, mixed> $rule
     * @return list<array{ dimension: string, operator: string, value: mixed }>
     */
    public static function normalizeConditions(array $rule): array
    {
        $conditions = [];

        foreach ($rule['conditions'] ?? [] as $dimension => $expected) {
            if ($dimension === 'amount') {
                if (\is_array($expected)) {
                    if (($expected['currency'] ?? null) !== null) {
                        $conditions[] = ['dimension' => 'amount_currency', 'operator' => 'eq', 'value' => $expected['currency']];
                    }
                    $operator = strtolower((string) ($expected['operator'] ?? 'eq'));
                    $conditions[] = ['dimension' => 'transaction_amount', 'operator' => $operator, 'value' => $expected['value'] ?? null];
                }
                continue;
            }

            if ($dimension === 'applies_to_markets') {
                $values = self::asList($expected);
                $hasAllOther = false;
                foreach ($values as $value) {
                    if (strcasecmp((string) $value, 'all_other_markets') === 0) {
                        $hasAllOther = true;
                        break;
                    }
                }
                if (!$hasAllOther) {
                    $conditions[] = ['dimension' => 'applies_to_markets_target', 'operator' => 'in', 'value' => $values];
                }
                continue;
            }

            if ($dimension === 'payment_methods') {
                $conditions[] = ['dimension' => 'payment_method', 'operator' => 'in', 'value' => self::asList($expected)];
                continue;
            }

            if (\is_array($expected)) {
                $conditions[] = ['dimension' => $dimension, 'operator' => 'in', 'value' => $expected];
            } else {
                $conditions[] = ['dimension' => $dimension, 'operator' => 'eq', 'value' => $expected];
            }
        }

        return $conditions;
    }

    /**
     * @param array{ dimension: string, operator: string, value: mixed } $condition
     * @param array<string, mixed> $context
     */
    public static function conditionStatus(array $condition, array $context): string
    {
        $actual = $context[$condition['dimension']] ?? null;
        $expected = $condition['value'];
        $operator = $condition['operator'];

        if ($actual === null && $expected !== null) {
            return 'missing';
        }

        if (\in_array($operator, ['eq', '==', 'equals'], true)) {
            if (\is_array($expected)) {
                foreach (self::asList($expected) as $item) {
                    if (self::valuesEqual($actual, $item)) {
                        return 'match';
                    }
                }
                return 'conflict';
            }
            return self::valuesEqual($actual, $expected) ? 'match' : 'conflict';
        }

        if (\in_array($operator, ['ne', '!=', 'not_equals'], true)) {
            if (\is_array($expected)) {
                foreach (self::asList($expected) as $item) {
                    if (self::valuesEqual($actual, $item)) {
                        return 'conflict';
                    }
                }
                return 'match';
            }
            return !self::valuesEqual($actual, $expected) ? 'match' : 'conflict';
        }

        if ($operator === 'in') {
            foreach (self::asList($expected) as $item) {
                if (self::valuesEqual($actual, $item)) {
                    return 'match';
                }
            }
            return 'conflict';
        }

        if (\in_array($operator, ['not_in', 'nin'], true)) {
            foreach (self::asList($expected) as $item) {
                if (self::valuesEqual($actual, $item)) {
                    return 'conflict';
                }
            }
            return 'match';
        }

        if (\in_array($operator, ['gt', 'gte', 'lt', 'lte'], true)) {
            return self::numericCompare((string) $actual, (string) $expected, $operator) ? 'match' : 'conflict';
        }

        throw new UnsupportedFeeShape('Unsupported condition operator: ' . $operator, ['operator' => $operator]);
    }

    public static function valuesEqual(mixed $left, mixed $right): bool
    {
        if (\is_bool($left) && \is_bool($right)) {
            return $left === $right;
        }

        if (\is_bool($left) || \is_bool($right)) {
            return false;
        }

        if (\is_string($left) && \is_string($right)) {
            return strcasecmp($left, $right) === 0;
        }

        if ((is_numeric($left) || \is_string($left) || $left instanceof BigDecimal) && (is_numeric($right) || \is_string($right) || $right instanceof BigDecimal)) {
            try {
                return BigDecimal::of((string) $left)->isEqualTo(BigDecimal::of((string) $right));
            } catch (\Throwable) {
                return false;
            }
        }

        return $left === $right;
    }

    /**
     * @return list<mixed>
     */
    public static function asList(mixed $value): array
    {
        return \is_array($value) ? $value : [$value];
    }

    private static function numericCompare(string $actual, string $expected, string $operator): bool
    {
        try {
            $left = BigDecimal::of($actual);
            $right = BigDecimal::of($expected);
        } catch (\Throwable) {
            throw new UnsupportedFeeShape('Numeric condition contains a non-numeric value.', ['actual' => $actual, 'expected' => $expected]);
        }

        $cmp = $left->compareTo($right);

        return match ($operator) {
            'gt' => $cmp > 0,
            'gte' => $cmp >= 0,
            'lt' => $cmp < 0,
            'lte' => $cmp <= 0,
            default => false,
        };
    }

    public static function apiFieldName(string $dimension): string
    {
        $mapping = [
            'product_id' => 'transaction.product_id',
            'variant_id' => 'transaction.variant_id',
            'payment_method' => 'transaction.payment_method',
            'transaction_region' => 'transaction.transaction_region',
            'payer_region' => 'transaction.payer_region',
            'surcharge_region' => 'transaction.surcharge_region',
            'applies_to_markets_target' => 'customer_country',
            'customer_country' => 'customer_country',
            'merchant_approval_required' => 'transaction.merchant_approval_required',
            'pricing_plan' => 'transaction.pricing_plan',
            'withdrawal_method' => 'transaction.withdrawal_method',
            'authorization_channel' => 'transaction.authorization_channel',
            'point_of_sale' => 'transaction.point_of_sale',
            'card_present' => 'transaction.card_present',
            'transaction_purpose' => 'transaction.transaction_purpose',
            'funding_source' => 'transaction.funding_source',
            'service' => 'transaction.service',
            'recipient_location' => 'transaction.recipient_location',
            'volume_status' => 'transaction.volume_status',
            'fee_currency' => 'transaction.fee_currency',
            'amount_currency' => 'amount.currency',
            'transaction_amount' => 'amount.value',
        ];

        return $mapping[$dimension] ?? 'transaction.context.' . $dimension;
    }

    /**
     * @param array<string, mixed> $rule
     */
    public static function specificity(array $rule): float
    {
        $score = 0.0;

        if (($rule['variant_id'] ?? null) !== null) {
            $score += 0.5;
        }

        foreach ($rule['conditions'] ?? [] as $dimension => $expected) {
            if ($dimension === 'amount') {
                $score += 1.0;
            } elseif ($dimension === 'applies_to_markets') {
                $values = self::asList($expected);
                $hasAllOther = false;
                foreach ($values as $value) {
                    if (strcasecmp((string) $value, 'all_other_markets') === 0) {
                        $hasAllOther = true;
                        break;
                    }
                }
                if ($hasAllOther) {
                    $score += 1.0;
                } else {
                    $score += 1.0 + (1.0 / max(\count($values), 1));
                }
            } elseif ($dimension === 'payment_methods') {
                $values = self::asList($expected);
                $score += 1.0 + (1.0 / max(\count($values), 1));
            } elseif ($dimension === 'pricing_plan') {
                $score += 2.0;
            } else {
                $score += 1.0;
            }
        }

        return $score;
    }

    /**
     * @param array<string, mixed> $rule
     */
    public static function isEvaluable(array $rule): bool
    {
        return ($rule['calculation_status'] ?? 'calculable') === 'calculable';
    }
}
