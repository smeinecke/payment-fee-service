<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Tests;

use PHPUnit\Framework\TestCase;
use Smeinecke\PaymentFee\Providers\Stripe\ConditionMatcher;

final class ConditionMatcherTest extends TestCase
{
    /**
     * @return list<array{bool|int|string, bool|int|string, bool}>
     */
    public static function strictBooleanEqualityProvider(): array
    {
        return [
            [true, true, true],
            [false, false, true],
            [true, false, false],
            [false, true, false],
            [true, 1, false],
            [false, 0, false],
            [1, true, false],
            [0, false, false],
            [true, 'true', false],
            [false, 'false', false],
            ['true', true, false],
            ['false', false, false],
        ];
    }

    /**
     * @dataProvider strictBooleanEqualityProvider
     */
    public function testBooleanEqualityIsStrict(bool|int|string $left, bool|int|string $right, bool $expected): void
    {
        $this->assertSame($expected, ConditionMatcher::valuesEqual($left, $right));
    }

    public function testStringEqualityIsCaseInsensitive(): void
    {
        $this->assertTrue(ConditionMatcher::valuesEqual('USD', 'usd'));
        $this->assertTrue(ConditionMatcher::valuesEqual('Domestic', 'DOMESTIC'));
        $this->assertFalse(ConditionMatcher::valuesEqual('us', 'eu'));
    }

    public function testNumericEqualityCoercesTypes(): void
    {
        $this->assertTrue(ConditionMatcher::valuesEqual('2.90', 2.9));
        $this->assertTrue(ConditionMatcher::valuesEqual(10, '10.0'));
        $this->assertTrue(ConditionMatcher::valuesEqual('1.10', 1.1));
        $this->assertFalse(ConditionMatcher::valuesEqual('1', '2'));
    }
}
