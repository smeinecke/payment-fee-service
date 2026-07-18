<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class BankContext
{
    public ?string $validation;
    public ?string $transferType;

    public function __construct(?string $validation = null, ?string $transferType = null)
    {
        $this->validation = $validation;
        $this->transferType = $transferType;
    }
}
