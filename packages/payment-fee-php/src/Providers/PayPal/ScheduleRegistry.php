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

    public function surchargeRate(string $scheduleId, ?string $payerRegion): ?string
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
                return ($entry['percentage_points'] ?? null) === null ? null : (string) $entry['percentage_points'];
            }
        }
        return null;
    }
}
