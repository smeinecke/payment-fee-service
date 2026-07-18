<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class PayPalTransaction
{
    public ?string $productId;
    public ?string $variantId;
    public ?string $paymentMethod;
    public ?string $paymentMethodVariant;
    public ?string $channel;
    public ?string $pricingPlan;
    public ?string $pricingTier;
    public ?string $payer;
    public string $unit;
    public ?bool $currencyConversionRequired;

    public ?string $transactionRegion;
    public ?string $payerRegion;
    public ?string $surchargeRegion;
    public ?bool $merchantApprovalRequired;
    public ?string $withdrawalMethod;
    public ?string $authorizationChannel;
    public ?bool $pointOfSale;
    public ?bool $cardPresent;
    public ?string $transactionPurpose;
    public ?string $fundingSource;
    public ?string $service;
    public ?string $recipientLocation;
    public ?string $volumeStatus;
    public ?string $feeCurrency;

    /**
     * @param array<string, mixed> $context
     */
    public function __construct(
        ?string $productId = null,
        ?string $variantId = null,
        ?string $paymentMethod = null,
        ?string $paymentMethodVariant = null,
        ?string $channel = null,
        ?string $pricingPlan = null,
        ?string $pricingTier = null,
        ?string $payer = null,
        string $unit = 'per_transaction',
        ?bool $currencyConversionRequired = null,
        ?string $transactionRegion = null,
        ?string $payerRegion = null,
        ?string $surchargeRegion = null,
        ?bool $merchantApprovalRequired = null,
        ?string $withdrawalMethod = null,
        ?string $authorizationChannel = null,
        ?bool $pointOfSale = null,
        ?bool $cardPresent = null,
        ?string $transactionPurpose = null,
        ?string $fundingSource = null,
        ?string $service = null,
        ?string $recipientLocation = null,
        ?string $volumeStatus = null,
        ?string $feeCurrency = null,
        public array $context = [],
    ) {
        $this->productId = $productId;
        $this->variantId = $variantId;
        $this->paymentMethod = $paymentMethod;
        $this->paymentMethodVariant = $paymentMethodVariant;
        $this->channel = $channel;
        $this->pricingPlan = $pricingPlan;
        $this->pricingTier = $pricingTier;
        $this->payer = $payer;
        $this->unit = $unit;
        $this->currencyConversionRequired = $currencyConversionRequired;
        $this->transactionRegion = $transactionRegion;
        $this->payerRegion = $payerRegion;
        $this->surchargeRegion = $surchargeRegion;
        $this->merchantApprovalRequired = $merchantApprovalRequired;
        $this->withdrawalMethod = $withdrawalMethod;
        $this->authorizationChannel = $authorizationChannel;
        $this->pointOfSale = $pointOfSale;
        $this->cardPresent = $cardPresent;
        $this->transactionPurpose = $transactionPurpose;
        $this->fundingSource = $fundingSource;
        $this->service = $service;
        $this->recipientLocation = $recipientLocation;
        $this->volumeStatus = $volumeStatus;
        $this->feeCurrency = $feeCurrency !== null ? strtoupper($feeCurrency) : null;
    }
}
