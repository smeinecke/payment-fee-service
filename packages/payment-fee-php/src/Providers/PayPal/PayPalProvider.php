<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\PayPal;

use Brick\Math\BigDecimal;
use Smeinecke\PaymentFee\Exception\AmbiguousFeeRules;
use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Exception\UnknownMarket;
use Smeinecke\PaymentFee\Exception\UnsupportedFeeShape;
use Smeinecke\PaymentFee\Model\ExecutableFeeRule;
use Smeinecke\PaymentFee\Model\PayPalQuoteRequest;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Providers\ProviderInterface;

final class PayPalProvider implements ProviderInterface
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
        $paypalRequest = $request;
        \assert($paypalRequest instanceof PayPalQuoteRequest);
        $country = $this->findCountry($paypalRequest->accountCountry);
        $registry = new ScheduleRegistry($country['derived'] ?? []);
        $rules = $country['derived']['transaction_fee_rules'] ?? [];

        $context = $this->buildContext($paypalRequest);
        $productId = $context['product_id'] ?? null;
        $variantId = $context['variant_id'] ?? null;

        $productRules = [];
        foreach ($rules as $rule) {
            if (strcasecmp((string) ($rule['id'] ?? ''), (string) $productId) !== 0) {
                continue;
            }
            if ($variantId !== null && ($rule['variant_id'] ?? null) !== null && strcasecmp((string) $rule['variant_id'], (string) $variantId) !== 0) {
                continue;
            }
            $productRules[] = $rule;
        }

        if ($productRules === []) {
            throw new QuoteNotAvailable(
                'The requested PayPal product/variant is not classified for this market.',
                ['market' => $paypalRequest->accountCountry, 'product_id' => $productId, 'variant_id' => $variantId],
            );
        }

        $fullMatches = [];
        $missingMatches = [];
        $allMissingFields = [];
        foreach ($productRules as $rule) {
            $conditions = ConditionMatcher::normalizeConditions($rule);
            $conflict = false;
            $missing = [];
            foreach ($conditions as $condition) {
                $status = ConditionMatcher::conditionStatus($condition, $context);
                if ($status === 'conflict') {
                    $conflict = true;
                    break;
                }
                if ($status === 'missing') {
                    $missing[] = ConditionMatcher::apiFieldName($condition['dimension']);
                }
            }
            if ($conflict) {
                continue;
            }

            $specificity = ConditionMatcher::specificity($rule);
            if ($missing !== []) {
                $missingMatches[] = ['rule' => $rule, 'specificity' => $specificity, 'missing' => $missing];
                foreach ($missing as $field) {
                    $allMissingFields[] = $field;
                }
            } else {
                $fullMatches[] = ['rule' => $rule, 'specificity' => $specificity];
            }
        }

        if ($fullMatches === []) {
            if ($missingMatches !== []) {
                $missingFields = array_values(array_unique($allMissingFields));
                sort($missingFields);
                throw new InsufficientTransactionContext(
                    $missingFields,
                    ['provider' => 'paypal', 'market' => $paypalRequest->accountCountry, 'product_id' => $productId, 'variant_id' => $variantId],
                );
            }
            throw new QuoteNotAvailable(
                'No fee rule matched the supplied context.',
                ['provider' => 'paypal', 'market' => $paypalRequest->accountCountry, 'product_id' => $productId, 'variant_id' => $variantId],
            );
        }

        $maxSpecificity = max(array_column($fullMatches, 'specificity'));
        $mostSpecificFull = array_values(array_filter($fullMatches, fn($m) => abs($m['specificity'] - $maxSpecificity) < 1e-9));

        if (!array_filter($mostSpecificFull, fn($m) => ConditionMatcher::isEvaluable($m['rule']))) {
            throw new QuoteNotAvailable(
                'A selected PayPal rule is not calculable.',
                [
                    'provider' => 'paypal',
                    'market' => $paypalRequest->accountCountry,
                    'rule_ids' => array_map(fn($m) => (string) $m['rule']['id'], $mostSpecificFull),
                    'product_id' => $productId,
                    'variant_id' => $variantId,
                ],
            );
        }

        $selectable = array_values(array_filter($fullMatches, fn($m) => ConditionMatcher::isEvaluable($m['rule'])));
        $maxSelectableSpec = max(array_column($selectable, 'specificity'));
        $selectedCandidates = array_values(array_filter($selectable, fn($m) => abs($m['specificity'] - $maxSelectableSpec) < 1e-9));

        if (\count($selectedCandidates) > 1) {
            $payerRegion = $context['payer_region'] ?? $context['surcharge_region'] ?? null;
            $signatures = [];
            foreach ($selectedCandidates as $candidate) {
                $signatures[] = $this->ruleSignature($candidate['rule'], $registry, $paypalRequest->amount->currency, $payerRegion);
            }

            $first = $signatures[0];
            for ($i = 1; $i < \count($signatures); ++$i) {
                if (!$this->signaturesEqual($first, $signatures[$i])) {
                    throw new AmbiguousFeeRules(
                        array_map(fn($m) => (string) $m['rule']['id'], $selectedCandidates),
                        ['provider' => 'paypal', 'market' => $paypalRequest->accountCountry, 'product_id' => $productId, 'variant_id' => $variantId],
                    );
                }
            }
        }

        usort($selectedCandidates, function ($a, $b) {
            $cmp = strcmp((string) $a['rule']['id'], (string) $b['rule']['id']);
            if ($cmp !== 0) {
                return $cmp;
            }
            return strcmp((string) ($a['rule']['variant_id'] ?? ''), (string) ($b['rule']['variant_id'] ?? ''));
        });
        $selected = $selectedCandidates[0]['rule'];

        $transactionRegion = strtolower((string) ($context['transaction_region'] ?? 'domestic'));
        $payerRegion = $context['payer_region'] ?? $context['surcharge_region'] ?? null;
        $surchargeScheduleId = $this->surchargeScheduleId($selected);

        if ($surchargeScheduleId !== null && $payerRegion === null && $transactionRegion !== 'domestic') {
            throw new InsufficientTransactionContext(
                ['transaction.payer_region', 'transaction.surcharge_region'],
                [
                    'provider' => 'paypal',
                    'market' => $paypalRequest->accountCountry,
                    'available_surcharge_regions' => array_values(array_unique($registry->surchargeRegions($surchargeScheduleId))),
                ],
            );
        }

        $compiled = $this->compileRule($selected, $registry, $paypalRequest, $context);
        return array_map(fn($rule) => $rule->toArray(), $compiled);
    }

    /**
     * @return list<ExecutableFeeRule>
     */
    private function compileRule(array $rule, ScheduleRegistry $registry, PayPalQuoteRequest $request, array $context): array
    {
        $currency = $request->amount->currency;
        $payerRegion = $context['payer_region'] ?? $context['surcharge_region'] ?? null;
        $transactionRegion = strtolower((string) ($context['transaction_region'] ?? 'domestic'));

        $fixedAmount = null;
        $fixedCurrency = null;

        foreach ($rule['fee_components'] ?? [] as $comp) {
            $type = $comp['type'] ?? '';
            if ($type === 'fixed_amount') {
                if (($comp['amount'] ?? null) !== null) {
                    $fixedAmount = ($fixedAmount === null ? BigDecimal::zero() : $fixedAmount)->plus(BigDecimal::of((string) $comp['amount']));
                    $fixedCurrency = $comp['currency'] ?? $fixedCurrency ?? $currency;
                }
            } elseif (\in_array($type, ['fixed_fee_schedule', 'international_surcharge_schedule', 'maximum_fee_schedule', 'percentage'], true)) {
                // handled below
            } else {
                throw new UnsupportedFeeShape('Unsupported PayPal fee component type: ' . (string) $type, ['rule_id' => $rule['id']]);
            }
        }

        $fixedScheduleId = $rule['fixed_fee_schedule'] ?? $this->componentScheduleId($rule, 'fixed_fee_schedule');
        if ($fixedScheduleId !== null) {
            $value = $registry->fixed($fixedScheduleId, $currency);
            $fixedAmount = ($fixedAmount === null ? BigDecimal::zero() : $fixedAmount)->plus(BigDecimal::of($value));
            $fixedCurrency = $currency;
        }

        $percentage = $rule['percentage'] ?? null;
        foreach ($rule['fee_components'] ?? [] as $comp) {
            if (($comp['type'] ?? '') === 'percentage' && ($comp['value'] ?? null) !== null) {
                $percentage = (string) $comp['value'];
            }
        }

        $maximumAmount = null;
        $maxScheduleId = $rule['maximum_fee_schedule'] ?? $this->componentScheduleId($rule, 'maximum_fee_schedule');
        if ($maxScheduleId !== null) {
            $maximumAmount = $registry->maximum($maxScheduleId, $currency);
        }

        $executable = new ExecutableFeeRule(
            rule_id: "paypal:{$request->accountCountry}:{$rule['id']}:" . ($rule['variant_id'] ?? 'default') . ':base',
            label: $rule['label'] ?? $rule['id'],
            component_type: 'processing',
            behavior: 'base',
            percentage: $percentage,
            fixed_amount: $fixedAmount === null ? null : (string) $fixedAmount,
            fixed_currency: $fixedAmount === null ? null : ($fixedCurrency ?? $currency),
            minimum_amount: null,
            maximum_amount: $maximumAmount,
            classification_status: $rule['calculation_status'] ?? 'calculable',
            confidence: 1.0,
            exactness: 'exact',
            source_url: null,
        );

        $surchargeScheduleId = $this->surchargeScheduleId($rule);
        if ($surchargeScheduleId === null) {
            return [$executable];
        }

        if ($payerRegion === null && $transactionRegion !== 'domestic') {
            throw new InsufficientTransactionContext(
                ['transaction.payer_region', 'transaction.surcharge_region'],
                [
                    'provider' => 'paypal',
                    'market' => $request->accountCountry,
                    'available_surcharge_regions' => array_values(array_unique($registry->surchargeRegions($surchargeScheduleId))),
                ],
            );
        }

        $surcharge = $registry->surcharge($surchargeScheduleId, $payerRegion, $currency);
        if ($surcharge === null || ($surcharge['percentage'] === null && $surcharge['fixed_amount'] === null)) {
            return [$executable];
        }

        $surchargeRule = new ExecutableFeeRule(
            rule_id: "paypal:{$request->accountCountry}:{$rule['id']}:" . ($rule['variant_id'] ?? 'default') . ':surcharge:' . ($payerRegion ?? 'unknown'),
            label: 'International surcharge (' . ($payerRegion ?? 'unknown') . ')',
            component_type: 'surcharge',
            behavior: 'additive',
            percentage: $surcharge['percentage'],
            fixed_amount: $surcharge['fixed_amount'],
            fixed_currency: $surcharge['fixed_amount'] !== null ? ($surcharge['fixed_currency'] ?? $currency) : null,
            classification_status: $rule['calculation_status'] ?? 'calculable',
            confidence: 1.0,
            exactness: 'exact',
            source_url: null,
        );

        return [$executable, $surchargeRule];
    }

    /**
     * @return array<string, mixed>
     */
    private function buildContext(PayPalQuoteRequest $request): array
    {
        $transaction = $request->transaction;

        $context = [
            'account_country' => $request->accountCountry,
            'customer_country' => $request->customerCountry,
            'amount_currency' => $request->amount->currency,
            'transaction_amount' => $request->amount->value,
            'product_id' => $transaction->productId,
            'variant_id' => $transaction->variantId,
            'payment_method' => $transaction->paymentMethod,
            'payer_region' => $transaction->payerRegion,
            'surcharge_region' => $transaction->surchargeRegion,
            'merchant_approval_required' => $transaction->merchantApprovalRequired,
            'pricing_plan' => $transaction->pricingPlan,
            'withdrawal_method' => $transaction->withdrawalMethod,
            'authorization_channel' => $transaction->authorizationChannel,
            'point_of_sale' => $transaction->pointOfSale,
            'card_present' => $transaction->cardPresent,
            'transaction_purpose' => $transaction->transactionPurpose,
            'funding_source' => $transaction->fundingSource,
            'service' => $transaction->service,
            'recipient_location' => $transaction->recipientLocation,
            'volume_status' => $transaction->volumeStatus,
            'fee_currency' => $transaction->feeCurrency ?? $request->amount->currency,
        ];

        if ($transaction->transactionRegion !== null) {
            $context['transaction_region'] = strtolower($transaction->transactionRegion);
        } elseif ($request->customerCountry !== null) {
            $context['transaction_region'] = $request->customerCountry === $request->accountCountry ? 'domestic' : 'international';
        } else {
            $context['transaction_region'] = 'domestic';
        }

        foreach ($transaction->context as $key => $value) {
            if (\array_key_exists($key, $context)) {
                if ($context[$key] === null) {
                    $context[$key] = $value;
                } elseif (!ConditionMatcher::valuesEqual($value, $context[$key])) {
                    throw new QuoteNotAvailable(
                        'Contradictory duplicate value in transaction context.',
                        ['field' => $key, 'typed_value' => $context[$key], 'context_value' => $value],
                    );
                }
            } else {
                $context[$key] = $value;
            }
        }

        $transactionRegion = strtolower((string) ($context['transaction_region'] ?? ''));
        if ($transactionRegion === 'international') {
            $context['applies_to_markets_target'] = $context['customer_country'] ?? null;
        } else {
            $context['applies_to_markets_target'] = $context['account_country'] ?? null;
        }

        return $context;
    }

    /**
     * @param array<string, mixed> $rule
     */
    private function componentScheduleId(array $rule, string $type): ?string
    {
        foreach ($rule['fee_components'] ?? [] as $comp) {
            if (($comp['type'] ?? '') === $type && !empty($comp['schedule_id'])) {
                return $comp['schedule_id'];
            }
        }

        return null;
    }

    /**
     * @param array<string, mixed> $rule
     */
    private function surchargeScheduleId(array $rule): ?string
    {
        return $rule['international_surcharge_schedule'] ?? $this->componentScheduleId($rule, 'international_surcharge_schedule');
    }

    /**
     * @param array<string, mixed> $rule
     * @return list<string|null>
     */
    private function ruleSignature(array $rule, ScheduleRegistry $registry, string $currency, ?string $payerRegion): array
    {
        $percentage = $rule['percentage'] ?? null;
        foreach ($rule['fee_components'] ?? [] as $comp) {
            if (($comp['type'] ?? '') === 'percentage' && ($comp['value'] ?? null) !== null) {
                $percentage = (string) $comp['value'];
            }
        }

        $fixedAmount = null;
        foreach ($rule['fee_components'] ?? [] as $comp) {
            if (($comp['type'] ?? '') === 'fixed_amount' && ($comp['amount'] ?? null) !== null) {
                $fixedAmount = ($fixedAmount === null ? BigDecimal::zero() : $fixedAmount)->plus(BigDecimal::of((string) $comp['amount']));
            }
        }

        $fixedScheduleId = $rule['fixed_fee_schedule'] ?? $this->componentScheduleId($rule, 'fixed_fee_schedule');
        if ($fixedScheduleId !== null) {
            try {
                $value = $registry->fixed($fixedScheduleId, $currency);
                $fixedAmount = ($fixedAmount === null ? BigDecimal::zero() : $fixedAmount)->plus(BigDecimal::of($value));
            } catch (QuoteNotAvailable) {
                // Missing currency leaves the fixed portion unspecified for comparison.
            }
        }

        $maxScheduleId = $rule['maximum_fee_schedule'] ?? $this->componentScheduleId($rule, 'maximum_fee_schedule');
        $maximumAmount = null;
        if ($maxScheduleId !== null) {
            try {
                $maximumAmount = $registry->maximum($maxScheduleId, $currency);
            } catch (QuoteNotAvailable) {
                // Missing currency leaves the maximum unspecified for comparison.
            }
        }

        $surchargeScheduleId = $this->surchargeScheduleId($rule);
        $surchargeRate = null;
        if ($surchargeScheduleId !== null && $payerRegion !== null) {
            $surcharge = $registry->surcharge($surchargeScheduleId, $payerRegion, $currency);
            if ($surcharge !== null && $surcharge['percentage'] !== null) {
                $surchargeRate = $surcharge['percentage'];
            }
        }

        return [
            $percentage,
            $fixedAmount === null ? null : (string) $fixedAmount,
            $maximumAmount,
            $surchargeRate,
        ];
    }

    /**
     * @param list<string|null> $a
     * @param list<string|null> $b
     */
    private function signaturesEqual(array $a, array $b): bool
    {
        foreach ($a as $i => $value) {
            if (!ConditionMatcher::valuesEqual($value, $b[$i] ?? null)) {
                return false;
            }
        }

        return true;
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

        foreach ($this->core['countries'] ?? [] as $country) {
            foreach ($country['derived']['transaction_fee_rules'] ?? [] as $rule) {
                $total += 1;
                $status = $rule['calculation_status'] ?? 'calculable';
                if ($status !== 'calculable') {
                    $skipped += 1;
                    continue;
                }
                $unsupported = false;
                foreach ($rule['fee_components'] ?? [] as $comp) {
                    if (!\in_array($comp['type'] ?? '', ['fixed_amount', 'fixed_fee_schedule', 'percentage', 'international_surcharge_schedule', 'maximum_fee_schedule'], true)) {
                        $unsupported = true;
                    }
                }
                if ($unsupported) {
                    $skipped += 1;
                    continue;
                }
                if (!empty($rule['conditions'])) {
                    $contextRequired += 1;
                }
                $parsed += 1;
            }
        }

        return [
            'paypal_calculable_rules_total' => $total,
            'paypal_calculable_rules_parsed' => $parsed,
            'paypal_calculable_rules_skipped' => $skipped,
            'paypal_context_required' => $contextRequired,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function findCountry(string $code): array
    {
        foreach ($this->core['countries'] ?? [] as $country) {
            if (strcasecmp((string) ($country['country_code'] ?? ''), $code) === 0) {
                return $country;
            }
        }
        throw new UnknownMarket('paypal', $code);
    }
}
