<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Brick\Math\BigDecimal;
use Brick\Math\RoundingMode;

final class Rounding
{
    public static function quantum(int $minorUnits): BigDecimal
    {
        return match ($minorUnits) {
            0 => BigDecimal::of('1'),
            3 => BigDecimal::of('0.001'),
            default => BigDecimal::of('0.01'),
        };
    }

    public static function roundMoney(BigDecimal $value, string $currency): BigDecimal
    {
        $quantum = self::quantum(Currency::minorUnits($currency));
        return $value->dividedBy($quantum, 0, RoundingMode::HALF_UP)
            ->multipliedBy($quantum)
            ->stripTrailingZeros();
    }

    public static function toString(BigDecimal $value, string $currency): string
    {
        $minor = Currency::minorUnits($currency);
        if ($minor === 0) {
            return (string) $value->toBigInteger();
        }
        $scale = $minor;
        $str = (string) $value->toScale($scale, RoundingMode::HALF_UP);
        if ($str === '-0' || $str === '-0.' . str_repeat('0', $scale)) {
            return '0' . ($scale > 0 ? '.' . str_repeat('0', $scale) : '');
        }
        return $str;
    }
}
