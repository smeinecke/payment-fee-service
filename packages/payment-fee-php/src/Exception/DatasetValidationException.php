<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class DatasetValidationException extends PaymentFeeException
{
    public function __construct(string $message, array $details = [])
    {
        parent::__construct(
            'DATASET_VALIDATION_ERROR',
            $message,
            $details,
        );
    }
}
