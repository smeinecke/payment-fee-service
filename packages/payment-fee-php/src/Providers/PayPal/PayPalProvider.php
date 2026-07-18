<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\PayPal;

use Brick\Math\BigDecimal;
use Smeinecke\PaymentFee\Exception\AmbiguousFeeRules;
use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Exception\UnsupportedFeeShape;
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

        $candidates = [];
        foreach ($rules as $rule) {
            if (($rule['calculation_status'] ?? 'calculable') !== 'calculable') {
                continue;
            }
            if (($rule['id'] ?? null) !== $paypalRequest->transaction->productId) {
                continue;
            }
            if (($rule['variant_id'] ?? null) !== null && $rule['variant_id'] !== $paypalRequest->transaction->variantId) {
                continue;
            }
            $candidates[] = $rule;
        }

        if ($candidates === []) {
            throw new QuoteNotAvailable('No matching PayPal fee rule found.', ['product_id' => $paypalRequest->transaction->productId, 'variant_id' => $paypalRequest->transaction->variantId]);
        }

        if (\count($candidates) > 1) {
            throw new AmbiguousFeeRules(array_map(fn($r) => (string) $r['id'], $candidates));
        }

        return $this->compileRule($candidates[0], $registry, $paypalRequest);
    }

    /**
     * @return list<array<string, mixed>>
     */
    private function compileRule(array $rule, ScheduleRegistry $registry, PayPalQuoteRequest $request): array
    {
        $currency = $request->amount->currency;
        $payerRegion = $request->transaction->payerRegion ?? $request->transaction->surchargeRegion;

        $fixedAmount = null;
        $fixedCurrency = null;

        foreach ($rule['fee_components'] ?? [] as $comp) {
            $type = $comp['type'] ?? '';
            if ($type === 'fixed_amount') {
                if ($comp['amount'] ?? null) {
                    $fixedAmount = BigDecimal::of($fixedAmount ?? '0')->plus(BigDecimal::of((string) $comp['amount']));
                    $fixedCurrency = $comp['currency'] ?? $currency;
                }
            } elseif ($type === 'fixed_fee_schedule') {
                $scheduleId = $comp['schedule_id'] ?? $rule['fixed_fee_schedule'] ?? null;
                if ($scheduleId) {
                    $value = $registry->fixed($scheduleId, $currency);
                    $fixedAmount = $fixedAmount === null ? BigDecimal::of($value) : $fixedAmount->plus(BigDecimal::of($value));
                    $fixedCurrency = $currency;
                }
            } elseif ($type === 'international_surcharge_schedule' || $type === 'maximum_fee_schedule' || $type === 'percentage') {
                // handled below
            } else {
                throw new UnsupportedFeeShape("Unsupported PayPal fee component type: {$type}", ['rule_id' => $rule['id']]);
            }
        }

        $percentage = $rule['percentage'] ?? null;
        foreach ($rule['fee_components'] ?? [] as $comp) {
            if (($comp['type'] ?? '') === 'percentage' && ($comp['value'] ?? null)) {
                $percentage = (string) $comp['value'];
            }
        }

        $maximumAmount = null;
        $scheduleId = $rule['maximum_fee_schedule'] ?? null;
        if ($scheduleId) {
            $maximumAmount = $registry->maximum($scheduleId, $currency);
        }

        $executable = [
            'rule_id' => "paypal:{$request->accountCountry}:{$rule['id']}:" . ($rule['variant_id'] ?? 'default') . ':base',
            'label' => $rule['label'] ?? $rule['id'],
            'component_type' => 'processing',
            'behavior' => 'base',
            'percentage' => $percentage,
            'fixed_amount' => $fixedAmount === null ? null : (string) $fixedAmount,
            'fixed_currency' => $fixedCurrency,
            'minimum_amount' => null,
            'maximum_amount' => $maximumAmount,
            'classification_status' => $rule['calculation_status'] ?? 'calculable',
            'confidence' => 1.0,
            'exactness' => 'exact',
            'source_url' => null,
        ];

        $surchargeScheduleId = $rule['international_surcharge_schedule'] ?? null;
        if ($surchargeScheduleId === null) {
            return [$executable];
        }

        if ($payerRegion === null && strcasecmp($request->transaction->transactionRegion ?? '', 'domestic') !== 0) {
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

        $surchargeRule = [
            'rule_id' => "paypal:{$request->accountCountry}:{$rule['id']}:" . ($rule['variant_id'] ?? 'default') . ':surcharge:' . ($payerRegion ?? 'unknown'),
            'label' => "International surcharge (" . ($payerRegion ?? 'unknown') . ")",
            'component_type' => 'surcharge',
            'behavior' => 'additive',
            'percentage' => $surcharge['percentage'],
            'fixed_amount' => $surcharge['fixed_amount'],
            'fixed_currency' => $surcharge['fixed_amount'] ? ($surcharge['fixed_currency'] ?? $currency) : null,
            'minimum_amount' => null,
            'maximum_amount' => null,
            'classification_status' => $rule['calculation_status'] ?? 'calculable',
            'confidence' => 1.0,
            'exactness' => 'exact',
            'source_url' => null,
        ];

        return [$executable, $surchargeRule];
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
        throw new QuoteNotAvailable('PayPal market not found.', ['market' => $code]);
    }
}
