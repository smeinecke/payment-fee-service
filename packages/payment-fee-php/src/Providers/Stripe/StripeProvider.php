<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\Stripe;

use Brick\Math\BigDecimal;
use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Exception\UnknownMarket;
use Smeinecke\PaymentFee\Exception\UnsupportedFeeShape;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Model\StripeQuoteRequest;
use Smeinecke\PaymentFee\Providers\ProviderInterface;

final class StripeProvider implements ProviderInterface
{
    private array $core;

    /**
     * @param array<string, mixed> $core
     */
    public function __construct(array $core)
    {
        $this->core = $core;
    }

    /**
     * @return list<array<string, mixed>>
     */
    public function compileRules(QuoteRequest $request): array
    {
        $stripeRequest = $request;
        \assert($stripeRequest instanceof StripeQuoteRequest);

        $market = $this->findMarket($stripeRequest->accountCountry);
        $context = $this->buildContext($stripeRequest);
        $currency = $stripeRequest->amount->currency;

        $fullMatches = [];
        $missingMatches = [];

        foreach ($market['rules'] ?? [] as $rule) {
            $conditions = $this->normalizeConditions($rule);
            $conflict = false;
            $missing = [];
            foreach ($conditions as $condition) {
                $status = $this->conditionStatus($condition, $context);
                if ($status === 'conflict') {
                    $conflict = true;
                    break;
                }
                if ($status === 'missing') {
                    $missing[] = $this->apiFieldName($condition['dimension']);
                }
            }
            if ($conflict) {
                continue;
            }
            $specificity = \count($conditions);
            if ($missing !== []) {
                $missingMatches[] = ['rule' => $rule, 'missing' => array_values(array_unique($missing)), 'specificity' => $specificity];
            } else {
                $fullMatches[] = ['rule' => $rule, 'specificity' => $specificity];
            }
        }

        if ($fullMatches === []) {
            if ($missingMatches !== []) {
                $allMissing = [];
                foreach ($missingMatches as $m) {
                    foreach ($m['missing'] as $field) {
                        $allMissing[$field] = true;
                    }
                }
                throw new InsufficientTransactionContext(array_keys($allMissing), ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
            }
            throw new QuoteNotAvailable('No Stripe fee rule matched the supplied context.', ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
        }

        $specificities = array_column($fullMatches, 'specificity');
        $maxSpec = max($specificities);
        $mostSpecific = array_filter($fullMatches, fn($m) => $m['specificity'] === $maxSpec);

        if (!$this->anyEvaluable(array_column($mostSpecific, 'rule'))) {
            throw new QuoteNotAvailable('The most specific matching Stripe fee rule cannot be quoted.', [
                'provider' => 'stripe',
                'market' => $stripeRequest->accountCountry,
                'rule_ids' => array_map(fn($m) => (string) $m['rule']['rule_id'], $mostSpecific),
            ]);
        }

        $baseCandidates = [];
        foreach ($this->sortBySpecificityDesc($fullMatches) as $match) {
            $rule = $match['rule'];
            if ($this->isEvaluable($rule) && ($rule['behavior'] ?? 'base') !== 'additive') {
                $baseCandidates[] = $rule;
            }
        }

        if ($baseCandidates === []) {
            throw new QuoteNotAvailable('No evaluable base Stripe fee rule matched.', ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
        }

        $base = $baseCandidates[0];
        $additiveRules = $this->selectAdditiveRules($market['rules'] ?? [], $context);

        $rules = [$this->executableFromRule($base, $currency)];
        foreach ($additiveRules as $rule) {
            $rules[] = $this->executableFromRule($rule, $currency);
        }

        return $rules;
    }

    /**
     * @return array<string, int>
     */
    public function auditContract(): array
    {
        $total = 0;
        $parsed = 0;
        $skipped = 0;
        $contextRequired = 0;

        foreach ($this->core['markets'] ?? [] as $market) {
            foreach ($market['rules'] ?? [] as $rule) {
                $total += 1;
                if (!$this->isEvaluable($rule)) {
                    $skipped += 1;
                    continue;
                }
                $hasComponents = ($rule['fee_components'] ?? []) !== [];
                $hasLegacy = ($rule['basis_points'] ?? null) !== null || ($rule['fixed_amount'] ?? null) !== null;
                if (!$hasComponents && !$hasLegacy) {
                    $skipped += 1;
                    continue;
                }
                if (($rule['conditions'] ?? []) !== []) {
                    $contextRequired += 1;
                }
                $parsed += 1;
            }
        }

        return [
            'stripe_calculable_rules_total' => $total,
            'stripe_calculable_rules_parsed' => $parsed,
            'stripe_calculable_rules_skipped' => $skipped,
            'stripe_context_required' => $contextRequired,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function findMarket(string $code): array
    {
        foreach ($this->core['markets'] ?? [] as $market) {
            if (strcasecmp((string) ($market['account_country'] ?? ''), $code) === 0) {
                return $market;
            }
        }
        throw new UnknownMarket('stripe', $code);
    }

    /**
     * @return array<string, mixed>
     */
    private function buildContext(StripeQuoteRequest $request): array
    {
        $t = $request->transaction;
        $context = [
            'account_country' => strtoupper($request->accountCountry),
            'customer_country' => $request->customerCountry !== null ? strtoupper($request->customerCountry) : null,
            'amount_currency' => strtoupper($request->amount->currency),
            'transaction_amount' => $request->amount->value,
            'presentment_currency' => strtoupper($request->amount->currency),
            'settlement_currency' => $request->settlementCurrency !== null ? strtoupper($request->settlementCurrency) : null,
            'product_id' => $t->productId,
            'variant_id' => $t->variantId,
            'payment_method' => $t->paymentMethod,
            'payment_method_variant' => $t->paymentMethodVariant,
            'channel' => $t->channel,
            'pricing_plan' => $t->pricingPlan,
            'pricing_tier' => $t->pricingTier,
            'payer' => $t->payer,
            'unit' => $t->unit ?? 'per_transaction',
            'currency_conversion_required' => $t->currencyConversionRequired,
            'recurring' => $t->recurring,
            'billing_type' => $t->billingType,
            'transaction_region' => $t->transactionRegion,
            'cross_border' => $t->crossBorder,
            'integration_type' => $t->integrationType,
            'product_feature' => $t->productFeature,
            'contract_length' => $t->contractLength,
            'feature_enabled' => $t->featureEnabled,
            'dispute_state' => $t->disputeState,
            'success' => true,
        ];

        if ($t->card !== null) {
            $context['card_origin'] = $t->card->origin;
            $context['card_region'] = $t->card->region;
            $context['card_type'] = $t->card->type;
            $context['card_network'] = $t->card->network;
            $context['card_tier'] = $t->card->tier;
            $context['card_entry_mode'] = $t->card->entryMode;
        }

        if ($t->settlement !== null) {
            if ($t->settlement->currency !== null) {
                $context['settlement_currency'] = strtoupper($t->settlement->currency);
            }
            $context['settlement_timing'] = $t->settlement->timing;
        }

        if ($t->bank !== null) {
            $context['bank_account_validation'] = $t->bank->validation;
            $context['bank_transfer_type'] = $t->bank->transferType;
        }

        if ($t->context !== null && \array_key_exists('success', $t->context)) {
            $context['success'] = $t->context['success'];
        }

        foreach ($t->context ?? [] as $key => $value) {
            if (($context[$key] ?? null) === null) {
                $context[$key] = $value;
            }
        }

        return $context;
    }

    /**
     * @param array<string, mixed> $rule
     * @return list<array{ dimension: string, operator: string, value: mixed }>
     */
    private function normalizeConditions(array $rule): array
    {
        $conditions = [];
        $topLevel = [
            ['account_country', $rule['account_country'] ?? null],
            ['payment_method', $rule['payment_method'] ?? null],
            ['payment_method_variant', $rule['payment_method_variant'] ?? null],
            ['product_id', $rule['product_id'] ?? null],
            ['variant_id', $rule['variant_id'] ?? null],
            ['channel', $rule['channel'] ?? null],
            ['card_origin', $rule['card_origin'] ?? null],
            ['card_region', $rule['card_region'] ?? null],
            ['card_tier', $rule['card_tier'] ?? null],
            ['card_type', $rule['card_type'] ?? null],
            ['card_network', $rule['card_network'] ?? null],
            ['card_entry_mode', $rule['card_entry_mode'] ?? null],
            ['customer_country', $rule['customer_country'] ?? null],
            ['presentment_currency', $rule['presentment_currency'] ?? null],
            ['settlement_currency', $rule['settlement_currency'] ?? null],
            ['settlement_timing', $rule['settlement_timing'] ?? null],
            ['currency_conversion_required', $rule['currency_conversion_required'] ?? null],
            ['recurring', $rule['recurring'] ?? null],
            ['billing_type', $rule['billing_type'] ?? null],
            ['pricing_plan', $rule['pricing_plan'] ?? null],
            ['pricing_tier', $rule['pricing_tier'] ?? null],
            ['product_feature', $rule['product_feature'] ?? null],
            ['integration_type', $rule['integration_type'] ?? null],
            ['contract_length', $rule['contract_length'] ?? null],
            ['dispute_state', $rule['dispute_state'] ?? null],
            ['transaction_region', $rule['transaction_region'] ?? null],
            ['transaction_type', $rule['transaction_type'] ?? null],
            ['cross_border', $rule['cross_border'] ?? null],
            ['feature_enabled', $rule['feature_enabled'] ?? null],
            ['payer', $rule['payer'] ?? null],
            ['success', $rule['success'] ?? null],
            ['bank_account_validation', $rule['bank_account_validation'] ?? null],
            ['bank_transfer_type', $rule['bank_transfer_type'] ?? null],
            ['fee_type', $rule['fee_type'] ?? null],
        ];

        foreach ($topLevel as [$dimension, $value]) {
            if ($value !== null) {
                $conditions[] = ['dimension' => $dimension, 'operator' => 'eq', 'value' => $value];
            }
        }

        if (($rule['transaction_amount_min'] ?? null) !== null) {
            $conditions[] = ['dimension' => 'transaction_amount', 'operator' => 'gte', 'value' => $rule['transaction_amount_min']];
        }
        if (($rule['transaction_amount_max'] ?? null) !== null) {
            $conditions[] = ['dimension' => 'transaction_amount', 'operator' => 'lte', 'value' => $rule['transaction_amount_max']];
        }

        foreach ($rule['conditions'] ?? [] as $condition) {
            $conditions[] = [
                'dimension' => $condition['dimension'],
                'operator' => strtolower((string) ($condition['operator'] ?? 'eq')),
                'value' => $condition['value'],
            ];
        }

        return $conditions;
    }

    /**
     * @param array{ dimension: string, operator: string, value: mixed } $condition
     * @param array<string, mixed> $context
     */
    private function conditionStatus(array $condition, array $context): string
    {
        $actual = $context[$condition['dimension']] ?? null;
        $expected = $condition['value'];
        $operator = $condition['operator'];

        if ($actual === null) {
            return $expected === null ? 'match' : 'missing';
        }

        if (\in_array($operator, ['eq', '==', 'equals'], true)) {
            if (\is_array($expected)) {
                foreach ($this->asList($expected) as $item) {
                    if ($this->valuesEqual($actual, $item)) {
                        return 'match';
                    }
                }
                return 'conflict';
            }
            return $this->valuesEqual($actual, $expected) ? 'match' : 'conflict';
        }
        if (\in_array($operator, ['ne', '!=', 'not_equals'], true)) {
            if (\is_array($expected)) {
                foreach ($this->asList($expected) as $item) {
                    if ($this->valuesEqual($actual, $item)) {
                        return 'conflict';
                    }
                }
                return 'match';
            }
            return !$this->valuesEqual($actual, $expected) ? 'match' : 'conflict';
        }
        if ($operator === 'in') {
            foreach ($this->asList($expected) as $item) {
                if ($this->valuesEqual($actual, $item)) {
                    return 'match';
                }
            }
            return 'conflict';
        }
        if (\in_array($operator, ['not_in', 'nin'], true)) {
            foreach ($this->asList($expected) as $item) {
                if ($this->valuesEqual($actual, $item)) {
                    return 'conflict';
                }
            }
            return 'match';
        }
        if (\in_array($operator, ['gt', 'gte', 'lt', 'lte'], true)) {
            return $this->numericCompare((string) $actual, (string) $expected, $operator) ? 'match' : 'conflict';
        }

        throw new UnsupportedFeeShape('Unsupported condition operator: ' . $operator, ['operator' => $operator]);
    }

    private function valuesEqual(mixed $left, mixed $right): bool
    {
        if (\is_bool($left) || \is_bool($right)) {
            return (bool) $left === (bool) $right;
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
    private function asList(mixed $value): array
    {
        return \is_array($value) ? $value : [$value];
    }

    private function numericCompare(string $actual, string $expected, string $operator): bool
    {
        $left = BigDecimal::of($actual);
        $right = BigDecimal::of($expected);
        $cmp = $left->compareTo($right);

        return match ($operator) {
            'gt' => $cmp > 0,
            'gte' => $cmp >= 0,
            'lt' => $cmp < 0,
            'lte' => $cmp <= 0,
            default => false,
        };
    }

    /**
     * @param array<string, mixed> $rule
     */
    private function isEvaluable(array $rule): bool
    {
        return \in_array($rule['classification_status'] ?? 'unclassified', ['calculable_rule', 'free', 'included'], true);
    }

    /**
     * @param list<array<string, mixed>> $rules
     */
    private function anyEvaluable(array $rules): bool
    {
        foreach ($rules as $rule) {
            if ($this->isEvaluable($rule)) {
                return true;
            }
        }
        return false;
    }

    /**
     * @param list<array{ rule: array<string, mixed>, specificity: int, missing?: list<string> }> $matches
     * @return list<array{ rule: array<string, mixed>, specificity: int, missing?: list<string> }>
     */
    private function sortBySpecificityDesc(array $matches): array
    {
        usort($matches, fn($a, $b) => $b['specificity'] <=> $a['specificity']);
        return $matches;
    }

    /**
     * @param list<array<string, mixed>> $rules
     * @param array<string, mixed> $context
     * @return list<array<string, mixed>>
     */
    private function selectAdditiveRules(array $rules, array $context): array
    {
        $selected = [];
        foreach ($rules as $rule) {
            if (($rule['behavior'] ?? 'base') !== 'additive') {
                continue;
            }
            $conditions = $this->normalizeConditions($rule);
            $match = true;
            foreach ($conditions as $condition) {
                if ($this->conditionStatus($condition, $context) !== 'match') {
                    $match = false;
                    break;
                }
            }
            if ($match && $this->isEvaluable($rule)) {
                $selected[] = $rule;
            }
        }
        return $selected;
    }

    /**
     * @param array<string, mixed> $rule
     * @return array<string, mixed>
     */
    private function executableFromRule(array $rule, string $currency): array
    {
        $compiled = $this->compileComponents($rule, $currency);
        $label = $rule['label'] ?? $rule['name'] ?? $rule['rule_id'];
        $componentType = 'processing';
        if ($compiled['behavior'] === 'additive') {
            $componentType = 'surcharge';
        } elseif ($compiled['behavior'] === 'included') {
            $componentType = 'included';
        }

        return [
            'rule_id' => $rule['rule_id'],
            'label' => $label,
            'component_type' => $componentType,
            'behavior' => $compiled['behavior'],
            'percentage' => $compiled['percentage'],
            'fixed_amount' => $compiled['fixed_amount'],
            'fixed_currency' => $compiled['fixed_amount'] !== null ? $currency : null,
            'minimum_amount' => $compiled['minimum_amount'],
            'maximum_amount' => $compiled['maximum_amount'],
            'classification_status' => $rule['classification_status'] ?? 'unclassified',
            'confidence' => $rule['confidence'] ?? 0.0,
            'exactness' => $rule['exactness'] ?? 'exact',
            'source_url' => $rule['source_url'] ?? null,
            'payer' => $rule['payer'] ?? null,
            'unit' => $rule['unit'] ?? 'per_transaction',
        ];
    }

    /**
     * @param array<string, mixed> $rule
     * @return array{ percentage: string|null, fixed_amount: string|null, minimum_amount: string|null, maximum_amount: string|null, behavior: string }
     */
    private function compileComponents(array $rule, string $currency): array
    {
        $basePercentage = BigDecimal::zero();
        $baseFixed = BigDecimal::zero();
        $additivePercentage = BigDecimal::zero();
        $additiveFixed = BigDecimal::zero();
        $minimumAmount = null;
        $maximumAmount = null;

        $components = $rule['fee_components'] ?? [];
        if ($components === [] && ($rule['basis_points'] ?? null) !== null) {
            $components[] = ['type' => 'percentage', 'basis_points' => $rule['basis_points']];
        }
        if ($components === [] && ($rule['fixed_amount'] ?? null) !== null) {
            $components[] = ['type' => 'fixed_amount', 'amount' => $rule['fixed_amount'], 'currency' => $rule['fixed_currency'] ?? $currency];
        }
        if (($rule['minimum_amount'] ?? null) !== null) {
            $components[] = ['type' => 'minimum_fee', 'amount' => $rule['minimum_amount'], 'currency' => $rule['fixed_currency'] ?? $currency];
        }
        if (($rule['maximum_amount'] ?? null) !== null) {
            $components[] = ['type' => 'maximum_fee', 'amount' => $rule['maximum_amount'], 'currency' => $rule['fixed_currency'] ?? $currency];
        }

        foreach ($components as $comp) {
            $type = $comp['type'];
            $behavior = $rule['behavior'] ?? 'base';
            if ($type === 'percentage') {
                $rate = $this->componentRate($comp);
                if ($behavior === 'additive') {
                    $additivePercentage = $additivePercentage->plus($rate);
                } else {
                    $basePercentage = $basePercentage->plus($rate);
                }
            } elseif ($type === 'fixed_amount' || $type === 'fixed_surcharge') {
                $fixed = $this->componentFixed($comp, $currency, (string) $rule['rule_id']);
                if ($behavior === 'additive' || $type === 'fixed_surcharge') {
                    $additiveFixed = $additiveFixed->plus($fixed);
                } else {
                    $baseFixed = $baseFixed->plus($fixed);
                }
            } elseif ($type === 'minimum_fee') {
                $minimumAmount = $this->componentFixed($comp, $currency, (string) $rule['rule_id']);
            } elseif ($type === 'maximum_fee') {
                $maximumAmount = $this->componentFixed($comp, $currency, (string) $rule['rule_id']);
            } elseif ($type === 'percentage_surcharge') {
                $additivePercentage = $additivePercentage->plus($this->componentRate($comp));
            } else {
                throw new UnsupportedFeeShape('Unsupported Stripe fee component type: ' . $type, ['rule_id' => $rule['rule_id'], 'type' => $type]);
            }
        }

        $behavior = \in_array($rule['classification_status'] ?? '', ['free', 'included'], true) ? 'included' : ($rule['behavior'] ?? 'base');

        if ($behavior === 'additive') {
            return [
                'percentage' => $additivePercentage->isZero() ? null : (string) $additivePercentage,
                'fixed_amount' => $additiveFixed->isZero() ? null : (string) $additiveFixed,
                'minimum_amount' => null,
                'maximum_amount' => null,
                'behavior' => $behavior,
            ];
        }

        if ($behavior === 'included') {
            return ['percentage' => null, 'fixed_amount' => null, 'minimum_amount' => null, 'maximum_amount' => null, 'behavior' => $behavior];
        }

        return [
            'percentage' => $basePercentage->isZero() ? null : (string) $basePercentage,
            'fixed_amount' => $baseFixed->isZero() ? null : (string) $baseFixed,
            'minimum_amount' => $minimumAmount !== null ? (string) $minimumAmount : null,
            'maximum_amount' => $maximumAmount !== null ? (string) $maximumAmount : null,
            'behavior' => $behavior,
        ];
    }

    /**
     * @param array<string, mixed> $comp
     */
    private function componentRate(array $comp): BigDecimal
    {
        if (($comp['basis_points'] ?? null) !== null) {
            return BigDecimal::of((string) $comp['basis_points'])->dividedBy(100);
        }
        if (($comp['value'] ?? null) !== null) {
            return BigDecimal::of((string) $comp['value']);
        }
        throw new UnsupportedFeeShape('Percentage component missing basis_points and value.', ['component' => $comp['type']]);
    }

    /**
     * @param array<string, mixed> $comp
     */
    private function componentFixed(array $comp, string $currency, string $ruleId): BigDecimal
    {
        if (!isset($comp['amount'])) {
            throw new UnsupportedFeeShape('Fixed component missing amount.', ['component' => $comp['type'], 'rule_id' => $ruleId]);
        }
        $compCurrency = strtoupper((string) ($comp['currency'] ?? $currency));
        if ($compCurrency !== strtoupper($currency)) {
            throw new QuoteNotAvailable('A selected Stripe fee rule uses a fixed amount in a different currency.', ['rule_id' => $ruleId, 'component_currency' => $compCurrency, 'transaction_currency' => $currency]);
        }
        return BigDecimal::of((string) $comp['amount']);
    }

    private function apiFieldName(string $dimension): string
    {
        $mapping = [
            'payment_method' => 'transaction.payment_method',
            'payment_method_variant' => 'transaction.payment_method_variant',
            'product_id' => 'transaction.product_id',
            'variant_id' => 'transaction.variant_id',
            'channel' => 'transaction.channel',
            'card_origin' => 'transaction.card.origin',
            'card_region' => 'transaction.card.region',
            'card_tier' => 'transaction.card.tier',
            'card_type' => 'transaction.card.type',
            'card_network' => 'transaction.card.network',
            'card_entry_mode' => 'transaction.card.entry_mode',
            'customer_country' => 'customer_country',
            'presentment_currency' => 'amount.currency',
            'settlement_currency' => 'settlement_currency',
            'settlement_timing' => 'transaction.settlement.timing',
            'currency_conversion_required' => 'transaction.currency_conversion_required',
            'recurring' => 'transaction.recurring',
            'billing_type' => 'transaction.billing_type',
            'pricing_plan' => 'transaction.pricing_plan',
            'pricing_tier' => 'transaction.pricing_tier',
            'product_feature' => 'transaction.product_feature',
            'integration_type' => 'transaction.integration_type',
            'contract_length' => 'transaction.contract_length',
            'dispute_state' => 'transaction.dispute_state',
            'transaction_region' => 'transaction.transaction_region',
            'transaction_type' => 'transaction.context.transaction_type',
            'cross_border' => 'transaction.cross_border',
            'feature_enabled' => 'transaction.feature_enabled',
            'payer' => 'transaction.payer',
            'success' => 'transaction.context.success',
            'bank_account_validation' => 'transaction.bank.validation',
            'bank_transfer_type' => 'transaction.bank.transfer_type',
            'fee_type' => 'transaction.context.fee_type',
            'transaction_amount' => 'amount.value',
        ];

        return $mapping[$dimension] ?? 'transaction.context.' . $dimension;
    }
}
