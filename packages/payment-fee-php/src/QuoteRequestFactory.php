<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\Model\Money;
use Smeinecke\PaymentFee\Model\PayPalQuoteRequest;
use Smeinecke\PaymentFee\Model\PayPalTransaction;
use Smeinecke\PaymentFee\Model\QuoteRequest;

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

        throw new UnknownProvider($provider ?? '');
    }
}
