<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

final class Currency
{
    private static ?array $currencies = null;

    public static function minorUnits(string $currency): int
    {
        self::load();
        return self::$currencies[$currency]['minor_units'] ?? 2;
    }

    private static function load(): void
    {
        if (self::$currencies !== null) {
            return;
        }
        $path = __DIR__ . '/../../../contracts/currencies.json';
        if (!is_file($path)) {
            $path = __DIR__ . '/../contracts/currencies.json';
        }
        $data = json_decode(file_get_contents($path), true);
        self::$currencies = $data ?? [];
    }
}
