<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class UnknownMarket extends PaymentFeeException
{
    public function __construct(string $provider, string $market, array $details = [])
    {
        parent::__construct(
            'UNKNOWN_MARKET',
            "Provider {$provider} has no published market {$market}.",
            ['provider' => $provider, 'market' => $market, ...$details],
        );
    }
}
