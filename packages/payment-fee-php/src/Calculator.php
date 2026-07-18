<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Brick\Math\BigDecimal;
use Smeinecke\PaymentFee\Exception\CurrencyMismatch;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Model\Money;

final class Calculator
{
    /**
     * @param array<int, array<string, mixed>> $rules
     */
    public function calculate(Money $amount, string $currency, array $rules): array
    {
        $components = [];
        $matchedRules = [];
        $rawTotal = BigDecimal::of('0');

        foreach ($rules as $rule) {
            if (in_array($rule['behavior'] ?? 'base', ['free', 'included', 'waived'], true)) {
                $components[] = [
                    'type' => 'included',
                    'label' => $rule['label'],
                    'amount' => '0',
                    'currency' => $currency,
                    'source_rule_id' => $rule['rule_id'],
                ];
                $matchedRules[] = $this->matchedRule($rule);
                continue;
            }

            $component = $this->calculateRule($amount, $currency, $rule);
            $components[] = $component;
            $rawTotal = $rawTotal->plus(BigDecimal::of($component['amount']));
            $matchedRules[] = $this->matchedRule($rule);
        }

        if ($components === []) {
            throw new QuoteNotAvailable('No calculable fee components were produced.');
        }

        $processingFee = Rounding::roundMoney($rawTotal, $currency);
        $net = Rounding::roundMoney(BigDecimal::of($amount->value)->minus($processingFee), $currency);

        return [
            'amount' => ['value' => Rounding::toString(BigDecimal::of($amount->value), $amount->currency), 'currency' => $amount->currency],
            'processing_fee' => ['value' => Rounding::toString($processingFee, $currency), 'currency' => $currency],
            'net_amount' => ['value' => Rounding::toString($net, $currency), 'currency' => $currency],
            'components' => $components,
            'matched_rules' => $matchedRules,
        ];
    }

    /**
     * @param array<string, mixed> $rule
     * @return array<string, mixed>
     */
    private function calculateRule(Money $amount, string $currency, array $rule): array
    {
        if (($rule['fixed_amount'] ?? null) !== null && ($rule['fixed_currency'] ?? $currency) !== $currency) {
            throw new CurrencyMismatch(
                'A selected fee rule uses a fixed amount in a different currency.',
                ['rule_id' => $rule['rule_id'], 'fixed_currency' => $rule['fixed_currency'], 'transaction_currency' => $currency],
            );
        }

        $raw = BigDecimal::of('0');
        $ratePercentage = null;

        if (($rule['basis_points'] ?? null) !== null) {
            $ratePercentage = BigDecimal::of($rule['basis_points'])->dividedBy(BigDecimal::of('100'), 8);
            $raw = $raw->plus(BigDecimal::of($amount->value)->multipliedBy(BigDecimal::of($rule['basis_points']))->dividedBy(BigDecimal::of('10000'), 8));
        } elseif (($rule['percentage'] ?? null) !== null) {
            $ratePercentage = BigDecimal::of($rule['percentage']);
            $raw = $raw->plus(BigDecimal::of($amount->value)->multipliedBy($ratePercentage)->dividedBy(BigDecimal::of('100'), 8));
        }

        if (($rule['fixed_amount'] ?? null) !== null) {
            $raw = $raw->plus(BigDecimal::of($rule['fixed_amount']));
        }

        $minimumApplied = false;
        $maximumApplied = false;

        if (($rule['minimum_amount'] ?? null) !== null && $raw->isLessThan(BigDecimal::of($rule['minimum_amount']))) {
            $raw = BigDecimal::of($rule['minimum_amount']);
            $minimumApplied = true;
        }

        if (($rule['maximum_amount'] ?? null) !== null && $raw->isGreaterThan(BigDecimal::of($rule['maximum_amount']))) {
            $raw = BigDecimal::of($rule['maximum_amount']);
            $maximumApplied = true;
        }

        $rounded = Rounding::roundMoney($raw, $currency);

        $component = [
            'type' => $rule['component_type'] ?? 'processing',
            'label' => $rule['label'],
            'amount' => Rounding::toString($rounded, $currency),
            'currency' => $currency,
            'minimum_applied' => $minimumApplied,
            'maximum_applied' => $maximumApplied,
            'source_rule_id' => $rule['rule_id'],
        ];

        if ($ratePercentage !== null) {
            $component['rate_percentage'] = $ratePercentage->toPlainString();
        }
        if (($rule['fixed_amount'] ?? null) !== null) {
            $component['fixed_amount'] = Rounding::toString(BigDecimal::of($rule['fixed_amount']), $rule['fixed_currency'] ?? $currency);
        }

        return $component;
    }

    /**
     * @param array<string, mixed> $rule
     * @return array<string, mixed>
     */
    private function matchedRule(array $rule): array
    {
        return [
            'rule_id' => $rule['rule_id'],
            'classification_status' => $rule['classification_status'] ?? 'calculable',
            'confidence' => $rule['confidence'] ?? 1.0,
            'exactness' => $rule['exactness'] ?? 'exact',
            'source_url' => $rule['source_url'] ?? null,
        ];
    }
}
