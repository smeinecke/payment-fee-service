<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class UnsupportedFeeShape extends PaymentFeeException
{
    public function __construct(string $message, array $details = [])
    {
        parent::__construct(
            'UNSUPPORTED_FEE_SHAPE',
            $message,
            $details,
        );
    }
}
