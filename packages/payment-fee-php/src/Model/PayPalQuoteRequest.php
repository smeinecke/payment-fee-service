<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class PayPalQuoteRequest extends QuoteRequest
{
    public PayPalTransaction $transaction;

    public function __construct(
        Money $amount,
        string $accountCountry,
        PayPalTransaction $transaction,
        ?string $customerCountry = null,
        ?string $settlementCurrency = null,
    ) {
        parent::__construct($amount, $accountCountry, $customerCountry, $settlementCurrency);
        $this->transaction = $transaction;
    }

    protected function provider(): string
    {
        return 'paypal';
    }
}
