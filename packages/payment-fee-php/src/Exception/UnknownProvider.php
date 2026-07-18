<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class UnknownProvider extends PaymentFeeException
{
    public function __construct(string $provider, array $details = [])
    {
        parent::__construct(
            'UNKNOWN_PROVIDER',
            "Unknown provider: {$provider}",
            ['provider' => $provider, ...$details],
        );
    }
}
