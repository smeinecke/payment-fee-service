<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

abstract readonly class QuoteRequest
{
    public string $provider;
    public Money $amount;
    public string $accountCountry;
    public ?string $customerCountry;
    public ?string $settlementCurrency;

    public function __construct(
        Money $amount,
        string $accountCountry,
        ?string $customerCountry = null,
        ?string $settlementCurrency = null,
    ) {
        $this->provider = $this->provider();
        $this->amount = $amount;
        $this->accountCountry = strtoupper($accountCountry);
        $this->customerCountry = $customerCountry !== null ? strtoupper($customerCountry) : null;
        $this->settlementCurrency = $settlementCurrency !== null ? strtoupper($settlementCurrency) : null;
    }

    abstract protected function provider(): string;
}
