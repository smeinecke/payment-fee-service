<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Tests;

use PHPUnit\Framework\TestCase;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Providers\PayPal\ScheduleRegistry;

final class ScheduleRegistryTest extends TestCase
{
    /**
     * @return array<string, mixed>
     */
    private function derived(): array
    {
        return [
            'fixed_fee_schedules' => [
                'fixed_DE' => ['entries' => ['EUR' => '0.35', 'USD' => '0.50']],
            ],
            'international_surcharge_schedules' => [
                'surcharge_DE' => [
                    'entries' => [
                        ['payer_region' => 'US', 'percentage_points' => '1.5'],
                        ['payer_region' => 'GB', 'percentage_points' => '1.0', 'fixed_amount' => '0.10', 'fixed_currency' => 'EUR'],
                    ],
                ],
            ],
            'maximum_fee_schedules' => [
                'max_DE' => ['entries' => ['EUR' => '5.00']],
            ],
        ];
    }

    public function testFixedScheduleReturnsCurrencyEntry(): void
    {
        $registry = new ScheduleRegistry($this->derived());
        $this->assertSame('0.35', $registry->fixed('fixed_DE', 'EUR'));
        $this->assertSame('0.50', $registry->fixed('fixed_DE', 'USD'));
    }

    public function testFixedScheduleMissingCurrencyThrows(): void
    {
        $registry = new ScheduleRegistry($this->derived());

        $this->expectException(QuoteNotAvailable::class);
        $registry->fixed('fixed_DE', 'GBP');
    }

    public function testSurchargeForKnownRegion(): void
    {
        $registry = new ScheduleRegistry($this->derived());

        $this->assertSame(['percentage' => '1.5', 'fixed_amount' => null, 'fixed_currency' => 'EUR'], $registry->surcharge('surcharge_DE', 'US', 'EUR'));
        $this->assertSame(['percentage' => '1.0', 'fixed_amount' => '0.10', 'fixed_currency' => 'EUR'], $registry->surcharge('surcharge_DE', 'GB', 'EUR'));
    }

    public function testSurchargeForUnknownRegionReturnsNull(): void
    {
        $registry = new ScheduleRegistry($this->derived());
        $this->assertNull($registry->surcharge('surcharge_DE', 'CA', 'EUR'));
    }

    public function testSurchargeRegions(): void
    {
        $registry = new ScheduleRegistry($this->derived());
        $this->assertSame(['US', 'GB'], $registry->surchargeRegions('surcharge_DE'));
    }

    public function testMaximumSchedule(): void
    {
        $registry = new ScheduleRegistry($this->derived());
        $this->assertSame('5.00', $registry->maximum('max_DE', 'EUR'));
    }
}
