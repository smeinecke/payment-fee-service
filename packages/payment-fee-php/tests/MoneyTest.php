<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Tests;

use PHPUnit\Framework\TestCase;
use Smeinecke\PaymentFee\Model\Money;

final class MoneyTest extends TestCase
{
    public function testMoneyNormalizesCurrencyAndValue(): void
    {
        $money = new Money('100.00', 'eur');
        $this->assertSame('100.00', $money->value);
        $this->assertSame('EUR', $money->currency);
    }

    public function testMoneySerializesToArray(): void
    {
        $money = new Money('0', 'jpy');
        $this->assertSame(['value' => '0', 'currency' => 'JPY'], $money->toArray());
    }
}
