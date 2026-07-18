<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\PayPal;

use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;

final class ScheduleRegistry
{
    /**
     * @param array<string, mixed> $derived
     */
    public function __construct(private readonly array $derived) {}

    /**
     * @return list<string>
     */
    public function surchargeRegions(string $scheduleId): array
    {
        $schedule = $this->derived['international_surcharge_schedules'][$scheduleId] ?? null;
        if ($schedule === null) {
            return [];
        }
        $regions = [];
        foreach ($schedule['entries'] ?? [] as $entry) {
            $regions[] = (string) ($entry['payer_region'] ?? '');
        }
        return $regions;
    }

    public function fixed(string $scheduleId, string $currency): string
    {
        $schedule = $this->derived['fixed_fee_schedules'][$scheduleId] ?? null;
        if ($schedule === null) {
            throw new QuoteNotAvailable('The selected PayPal fee category has no fixed-fee schedule.', ['schedule_id' => $scheduleId]);
        }
        $entries = $schedule['entries'] ?? [];
        if (!isset($entries[$currency])) {
            throw new QuoteNotAvailable('No PayPal fixed fee is published for the transaction currency.', ['schedule_id' => $scheduleId, 'currency' => $currency]);
        }
        return (string) $entries[$currency];
    }

    public function maximum(string $scheduleId, string $currency): string
    {
        $schedule = $this->derived['maximum_fee_schedules'][$scheduleId] ?? null;
        if ($schedule === null) {
            throw new QuoteNotAvailable('The selected PayPal fee category has no maximum-fee schedule.', ['schedule_id' => $scheduleId]);
        }
        $entries = $schedule['entries'] ?? [];
        if (!isset($entries[$currency])) {
            throw new QuoteNotAvailable('No PayPal maximum fee is published for the transaction currency.', ['schedule_id' => $scheduleId, 'currency' => $currency]);
        }
        return (string) $entries[$currency];
    }

    /**
     * @return array{percentage: string|null, fixed_amount: string|null, fixed_currency: string|null}|null
     */
    public function surcharge(string $scheduleId, ?string $payerRegion, string $currency): ?array
    {
        if ($payerRegion === null) {
            return null;
        }
        $schedule = $this->derived['international_surcharge_schedules'][$scheduleId] ?? null;
        if ($schedule === null) {
            return null;
        }
        foreach ($schedule['entries'] ?? [] as $entry) {
            if (strcasecmp((string) ($entry['payer_region'] ?? ''), $payerRegion) === 0) {
                $fixedCurrency = $entry['fixed_currency'] ?? $currency;
                if (($entry['fixed_amount'] ?? null) !== null && strtoupper($fixedCurrency) !== strtoupper($currency)) {
                    throw new QuoteNotAvailable('A PayPal international surcharge schedule uses a fixed amount in a different currency.', [
                        'schedule_id' => $scheduleId,
                        'currency' => $currency,
                        'fixed_currency' => $fixedCurrency,
                    ]);
                }

                return [
                    'percentage' => ($entry['percentage_points'] ?? null) === null ? null : (string) $entry['percentage_points'],
                    'fixed_amount' => ($entry['fixed_amount'] ?? null) === null ? null : (string) $entry['fixed_amount'],
                    'fixed_currency' => $fixedCurrency,
                ];
            }
        }
        return null;
    }
}
