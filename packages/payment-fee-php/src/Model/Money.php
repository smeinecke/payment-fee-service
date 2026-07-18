<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

use Brick\Math\BigDecimal;

final readonly class Money
{
    public string $value;
    public string $currency;

    public function __construct(string $value, string $currency)
    {
        $this->value = (string) BigDecimal::of($value);
        $this->currency = strtoupper($currency);
    }

    public function toArray(): array
    {
        return [
            'value' => $this->value,
            'currency' => $this->currency,
        ];
    }
}
