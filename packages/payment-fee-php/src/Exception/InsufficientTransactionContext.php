<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class InsufficientTransactionContext extends PaymentFeeException
{
    /**
     * @param list<string> $missingFields
     */
    public function __construct(array $missingFields, array $details = [])
    {
        parent::__construct(
            'INSUFFICIENT_TRANSACTION_CONTEXT',
            'Additional transaction context is required to select an applicable fee rule.',
            ['missing_fields' => array_values(array_unique($missingFields)), ...$details],
        );
    }
}
