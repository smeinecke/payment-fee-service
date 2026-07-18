<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class StripeQuoteRequest extends QuoteRequest
{
    public StripeTransaction $transaction;

    public function __construct(
        Money $amount,
        string $accountCountry,
        StripeTransaction $transaction,
        ?string $customerCountry = null,
        ?string $settlementCurrency = null,
    ) {
        parent::__construct($amount, $accountCountry, $customerCountry, $settlementCurrency);
        $this->transaction = $transaction;
    }

    protected function provider(): string
    {
        return 'stripe';
    }
}
