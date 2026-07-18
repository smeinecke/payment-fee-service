<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

use RuntimeException;

abstract class PaymentFeeException extends RuntimeException
{
    public function __construct(
        public readonly string $errorCode,
        string $message,
        public readonly array $details = [],
    ) {
        parent::__construct($message);
    }
}
