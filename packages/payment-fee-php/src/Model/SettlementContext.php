<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class SettlementContext
{
    public ?string $currency;
    public ?string $timing;

    public function __construct(?string $currency = null, ?string $timing = null)
    {
        $this->currency = $currency !== null ? strtoupper($currency) : null;
        $this->timing = $timing;
    }
}
