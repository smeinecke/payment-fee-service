<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Tests;

use PHPUnit\Framework\TestCase;
use Smeinecke\PaymentFee\Calculator;
use Smeinecke\PaymentFee\Exception\CurrencyMismatch;
use Smeinecke\PaymentFee\Model\Money;

final class CalculatorTest extends TestCase
{
    public function testPercentageAndFixed(): void
    {
        $calculator = new Calculator();
        $result = $calculator->calculate(new Money('100.00', 'USD'), 'USD', [
            [
                'rule_id' => 'r1',
                'label' => '2.9% + 30c',
                'component_type' => 'processing',
                'behavior' => 'base',
                'percentage' => '2.9',
                'fixed_amount' => '0.30',
                'fixed_currency' => 'USD',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);

        $this->assertSame('3.20', $result['processing_fee']['value']);
        $this->assertSame('96.80', $result['net_amount']['value']);
        $this->assertSame('0.30', $result['components'][0]['fixed_amount']);
    }

    public function testMinimumFeeApplied(): void
    {
        $calculator = new Calculator();
        $result = $calculator->calculate(new Money('5.00', 'USD'), 'USD', [
            [
                'rule_id' => 'r1',
                'label' => '2% with minimum',
                'component_type' => 'processing',
                'behavior' => 'base',
                'percentage' => '2.0',
                'minimum_amount' => '0.50',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);

        $this->assertSame('0.50', $result['processing_fee']['value']);
        $this->assertTrue($result['components'][0]['minimum_applied']);
    }

    public function testMaximumFeeApplied(): void
    {
        $calculator = new Calculator();
        $result = $calculator->calculate(new Money('1000.00', 'USD'), 'USD', [
            [
                'rule_id' => 'r1',
                'label' => '2% with cap',
                'component_type' => 'processing',
                'behavior' => 'base',
                'percentage' => '2.0',
                'maximum_amount' => '5.00',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);

        $this->assertSame('5.00', $result['processing_fee']['value']);
        $this->assertTrue($result['components'][0]['maximum_applied']);
    }

    public function testCurrencyMismatchThrows(): void
    {
        $calculator = new Calculator();

        $this->expectException(CurrencyMismatch::class);
        $calculator->calculate(new Money('100.00', 'USD'), 'USD', [
            [
                'rule_id' => 'r1',
                'label' => 'fixed in EUR',
                'component_type' => 'processing',
                'behavior' => 'base',
                'fixed_amount' => '0.30',
                'fixed_currency' => 'EUR',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);
    }

    public function testAdditiveSurcharge(): void
    {
        $calculator = new Calculator();
        $result = $calculator->calculate(new Money('100.00', 'USD'), 'USD', [
            [
                'rule_id' => 'r1',
                'label' => '2.9% + 30c',
                'component_type' => 'processing',
                'behavior' => 'base',
                'percentage' => '2.9',
                'fixed_amount' => '0.30',
                'fixed_currency' => 'USD',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
            [
                'rule_id' => 'r2',
                'label' => 'Surcharge',
                'component_type' => 'surcharge',
                'behavior' => 'additive',
                'percentage' => '0.5',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);

        $this->assertSame('3.70', $result['processing_fee']['value']);
        $this->assertSame('96.30', $result['net_amount']['value']);
    }

    public function testZeroDecimalCurrency(): void
    {
        $calculator = new Calculator();
        $result = $calculator->calculate(new Money('1000', 'JPY'), 'JPY', [
            [
                'rule_id' => 'r1',
                'label' => '3.6%',
                'component_type' => 'processing',
                'behavior' => 'base',
                'percentage' => '3.6',
                'classification_status' => 'calculable_rule',
                'exactness' => 'exact',
            ],
        ]);

        $this->assertSame('36', $result['processing_fee']['value']);
        $this->assertSame('JPY', $result['processing_fee']['currency']);
    }
}
