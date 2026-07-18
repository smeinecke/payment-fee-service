<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class QuoteNotAvailable extends PaymentFeeException
{
    public function __construct(string $message, array $details = [])
    {
        parent::__construct(
            'QUOTE_NOT_AVAILABLE',
            $message,
            $details,
        );
    }
}
