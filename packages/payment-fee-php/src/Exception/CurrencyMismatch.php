<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class CurrencyMismatch extends PaymentFeeException
{
    public function __construct(string $message, array $details = [])
    {
        parent::__construct(
            'CURRENCY_MISMATCH',
            $message,
            $details,
        );
    }
}
