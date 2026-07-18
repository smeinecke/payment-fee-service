<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class ProviderDataUnavailable extends PaymentFeeException
{
    public function __construct(string $provider, string $reason, array $details = [])
    {
        parent::__construct(
            'PROVIDER_DATA_UNAVAILABLE',
            "Validated data for {$provider} is unavailable.",
            ['provider' => $provider, 'reason' => $reason, ...$details],
        );
    }
}
