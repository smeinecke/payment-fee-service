<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class StripeTransaction
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

    public ?CardContext $card;
    public ?SettlementContext $settlement;
    public ?BankContext $bank;
    public ?bool $recurring;
    public ?string $billingType;
    public ?string $transactionRegion;
    public ?string $customerCountry;
    public ?string $presentmentCurrency;
    public ?string $settlementCurrency;
    public ?string $settlementTiming;
    public ?string $bankAccountValidation;
    public ?string $integrationType;
    public ?string $productFeature;
    public ?string $contractLength;
    public ?bool $crossBorder;
    public ?string $featureEnabled;
    public ?string $disputeState;
    public ?string $cardTier;
    public ?string $cardType;
    public ?string $cardNetwork;
    public ?string $cardOrigin;
    public ?string $cardRegion;
    public ?string $cardEntryMode;

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
        ?CardContext $card = null,
        ?SettlementContext $settlement = null,
        ?BankContext $bank = null,
        ?bool $recurring = null,
        ?string $billingType = null,
        ?string $transactionRegion = null,
        ?string $customerCountry = null,
        ?string $presentmentCurrency = null,
        ?string $settlementCurrency = null,
        ?string $settlementTiming = null,
        ?string $bankAccountValidation = null,
        ?string $integrationType = null,
        ?string $productFeature = null,
        ?string $contractLength = null,
        ?bool $crossBorder = null,
        ?string $featureEnabled = null,
        ?string $disputeState = null,
        ?string $cardTier = null,
        ?string $cardType = null,
        ?string $cardNetwork = null,
        ?string $cardOrigin = null,
        ?string $cardRegion = null,
        ?string $cardEntryMode = null,
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
        $this->card = $card;
        $this->settlement = $settlement;
        $this->bank = $bank;
        $this->recurring = $recurring;
        $this->billingType = $billingType;
        $this->transactionRegion = $transactionRegion;
        $this->customerCountry = $customerCountry !== null ? strtoupper($customerCountry) : null;
        $this->presentmentCurrency = $presentmentCurrency !== null ? strtoupper($presentmentCurrency) : null;
        $this->settlementCurrency = $settlementCurrency !== null ? strtoupper($settlementCurrency) : null;
        $this->settlementTiming = $settlementTiming;
        $this->bankAccountValidation = $bankAccountValidation;
        $this->integrationType = $integrationType;
        $this->productFeature = $productFeature;
        $this->contractLength = $contractLength;
        $this->crossBorder = $crossBorder;
        $this->featureEnabled = $featureEnabled;
        $this->disputeState = $disputeState;
        $this->cardTier = $cardTier;
        $this->cardType = $cardType;
        $this->cardNetwork = $cardNetwork;
        $this->cardOrigin = $cardOrigin;
        $this->cardRegion = $cardRegion;
        $this->cardEntryMode = $cardEntryMode;
    }
}
