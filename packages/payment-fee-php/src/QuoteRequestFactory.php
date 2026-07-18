<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\Model\BankContext;
use Smeinecke\PaymentFee\Model\CardContext;
use Smeinecke\PaymentFee\Model\Money;
use Smeinecke\PaymentFee\Model\PayPalQuoteRequest;
use Smeinecke\PaymentFee\Model\PayPalTransaction;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Model\SettlementContext;
use Smeinecke\PaymentFee\Model\StripeQuoteRequest;
use Smeinecke\PaymentFee\Model\StripeTransaction;

final class QuoteRequestFactory
{
    public static function fromArray(array $data): QuoteRequest
    {
        $provider = $data['provider'] ?? null;
        if ($provider === 'paypal') {
            $tx = $data['transaction'] ?? [];

            return new PayPalQuoteRequest(
                new Money($data['amount']['value'], $data['amount']['currency']),
                $data['account_country'],
                new PayPalTransaction(
                    productId: $tx['product_id'] ?? null,
                    variantId: $tx['variant_id'] ?? null,
                    paymentMethod: $tx['payment_method'] ?? null,
                    paymentMethodVariant: $tx['payment_method_variant'] ?? null,
                    channel: $tx['channel'] ?? null,
                    pricingPlan: $tx['pricing_plan'] ?? null,
                    pricingTier: $tx['pricing_tier'] ?? null,
                    payer: $tx['payer'] ?? null,
                    unit: $tx['unit'] ?? 'per_transaction',
                    currencyConversionRequired: $tx['currency_conversion_required'] ?? null,
                    transactionRegion: $tx['transaction_region'] ?? null,
                    payerRegion: $tx['payer_region'] ?? null,
                    surchargeRegion: $tx['surcharge_region'] ?? null,
                    merchantApprovalRequired: $tx['merchant_approval_required'] ?? null,
                    withdrawalMethod: $tx['withdrawal_method'] ?? null,
                    authorizationChannel: $tx['authorization_channel'] ?? null,
                    pointOfSale: $tx['point_of_sale'] ?? null,
                    cardPresent: $tx['card_present'] ?? null,
                    transactionPurpose: $tx['transaction_purpose'] ?? null,
                    fundingSource: $tx['funding_source'] ?? null,
                    service: $tx['service'] ?? null,
                    recipientLocation: $tx['recipient_location'] ?? null,
                    volumeStatus: $tx['volume_status'] ?? null,
                    feeCurrency: $tx['fee_currency'] ?? null,
                    context: $tx['context'] ?? [],
                ),
                customerCountry: $data['customer_country'] ?? null,
                settlementCurrency: $data['settlement_currency'] ?? null,
            );
        }

        if ($provider === 'stripe') {
            $tx = $data['transaction'] ?? [];

            return new StripeQuoteRequest(
                new Money($data['amount']['value'], $data['amount']['currency']),
                $data['account_country'],
                new StripeTransaction(
                    productId: $tx['product_id'] ?? null,
                    variantId: $tx['variant_id'] ?? null,
                    paymentMethod: $tx['payment_method'] ?? null,
                    paymentMethodVariant: $tx['payment_method_variant'] ?? null,
                    channel: $tx['channel'] ?? null,
                    pricingPlan: $tx['pricing_plan'] ?? null,
                    pricingTier: $tx['pricing_tier'] ?? null,
                    payer: $tx['payer'] ?? null,
                    unit: $tx['unit'] ?? 'per_transaction',
                    currencyConversionRequired: $tx['currency_conversion_required'] ?? null,
                    card: isset($tx['card']) ? new CardContext(
                        origin: $tx['card']['origin'] ?? null,
                        region: $tx['card']['region'] ?? null,
                        type: $tx['card']['type'] ?? null,
                        network: $tx['card']['network'] ?? null,
                        tier: $tx['card']['tier'] ?? null,
                        entryMode: $tx['card']['entry_mode'] ?? null,
                    ) : null,
                    settlement: isset($tx['settlement']) ? new SettlementContext(
                        currency: $tx['settlement']['currency'] ?? null,
                        timing: $tx['settlement']['timing'] ?? null,
                    ) : null,
                    bank: isset($tx['bank']) ? new BankContext(
                        validation: $tx['bank']['validation'] ?? null,
                        transferType: $tx['bank']['transfer_type'] ?? null,
                    ) : null,
                    recurring: $tx['recurring'] ?? null,
                    billingType: $tx['billing_type'] ?? null,
                    transactionRegion: $tx['transaction_region'] ?? null,
                    customerCountry: $data['customer_country'] ?? null,
                    presentmentCurrency: $data['amount']['currency'] ?? null,
                    settlementCurrency: $data['settlement_currency'] ?? null,
                    settlementTiming: $tx['settlement']['timing'] ?? null,
                    bankAccountValidation: $tx['bank_account_validation'] ?? null,
                    integrationType: $tx['integration_type'] ?? null,
                    productFeature: $tx['product_feature'] ?? null,
                    contractLength: $tx['contract_length'] ?? null,
                    crossBorder: $tx['cross_border'] ?? null,
                    featureEnabled: $tx['feature_enabled'] ?? null,
                    disputeState: $tx['dispute_state'] ?? null,
                    cardTier: $tx['card_tier'] ?? null,
                    cardType: $tx['card_type'] ?? null,
                    cardNetwork: $tx['card_network'] ?? null,
                    cardOrigin: $tx['card_origin'] ?? null,
                    cardRegion: $tx['card_region'] ?? null,
                    cardEntryMode: $tx['card_entry_mode'] ?? null,
                    context: $tx['context'] ?? [],
                ),
                customerCountry: $data['customer_country'] ?? null,
                settlementCurrency: $data['settlement_currency'] ?? null,
            );
        }

        throw new UnknownProvider($provider ?? '');
    }
}
