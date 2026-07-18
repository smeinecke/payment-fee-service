<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Tests;

use PHPUnit\Framework\TestCase;
use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\PaymentFeeEngine;

final class PaymentFeeEngineTest extends TestCase
{
    /**
     * @return array<string, mixed>
     */
    private function paypalCore(): array
    {
        return [
            'schema_version' => 1,
            'countries' => [
                [
                    'country_code' => 'DE',
                    'derived' => [
                        'status' => 'calculable',
                        'transaction_fee_rules' => [
                            [
                                'id' => 'other_commercial',
                                'variant_id' => 'standard',
                                'label' => 'Commercial transaction',
                                'percentage' => '2.49',
                                'fixed_fee_schedule' => 'fixed__applies_to_markets=DE',
                                'international_surcharge_schedule' => 'surcharge__applies_to_markets=DE',
                                'calculation_status' => 'calculable',
                                'fee_components' => [
                                    ['type' => 'percentage'],
                                    ['type' => 'fixed_fee_schedule'],
                                    ['type' => 'international_surcharge_schedule'],
                                ],
                                'conditions' => [],
                            ],
                        ],
                        'fixed_fee_schedules' => [
                            'fixed__applies_to_markets=DE' => ['entries' => ['EUR' => '0.35']],
                        ],
                        'international_surcharge_schedules' => [
                            'surcharge__applies_to_markets=DE' => [
                                'entries' => [
                                    ['payer_region' => 'US', 'percentage_points' => '1.5'],
                                    ['payer_region' => 'GB', 'percentage_points' => '1.0', 'fixed_amount' => '0.10', 'fixed_currency' => 'EUR'],
                                ],
                            ],
                        ],
                        'maximum_fee_schedules' => [],
                    ],
                ],
            ],
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function stripeCore(): array
    {
        return [
            'schema_version' => 1,
            'markets' => [
                [
                    'account_country' => 'US',
                    'rules' => [
                        [
                            'rule_id' => 'stripe:US:card:base',
                            'provider' => 'stripe',
                            'account_country' => 'US',
                            'classification_status' => 'calculable_rule',
                            'behavior' => 'base',
                            'product_id' => 'payment',
                            'variant_id' => 'card',
                            'payment_method' => 'card',
                            'label' => 'Card payment',
                            'unit' => 'per_transaction',
                            'conditions' => [],
                            'fee_components' => [
                                ['type' => 'percentage', 'value' => '2.9'],
                                ['type' => 'fixed_amount', 'amount' => '0.30', 'currency' => 'USD'],
                            ],
                        ],
                        [
                            'rule_id' => 'stripe:US:card:manual',
                            'provider' => 'stripe',
                            'account_country' => 'US',
                            'classification_status' => 'calculable_rule',
                            'behavior' => 'additive',
                            'product_id' => 'payment',
                            'variant_id' => 'card',
                            'payment_method' => 'card',
                            'label' => 'Manually entered card surcharge',
                            'unit' => 'per_transaction',
                            'conditions' => [
                                ['dimension' => 'card_entry_mode', 'operator' => 'eq', 'value' => 'manual'],
                            ],
                            'fee_components' => [
                                ['type' => 'percentage_surcharge', 'value' => '0.5'],
                            ],
                        ],
                    ],
                ],
            ],
        ];
    }

    public function testPayPalQuoteWithoutSurcharge(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(paypal: $this->paypalCore());
        $result = $engine->quote([
            'provider' => 'paypal',
            'amount' => ['value' => '100.00', 'currency' => 'EUR'],
            'account_country' => 'DE',
            'transaction' => [
                'product_id' => 'other_commercial',
                'variant_id' => 'standard',
                'transaction_region' => 'domestic',
            ],
        ]);

        $this->assertSame('paypal', $result['provider']);
        $this->assertSame('2.84', $result['processing_fee']['value']);
        $this->assertSame('EUR', $result['processing_fee']['currency']);
        $this->assertSame('97.16', $result['net_amount']['value']);
    }

    public function testPayPalQuoteWithSurcharge(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(paypal: $this->paypalCore());
        $result = $engine->quote([
            'provider' => 'paypal',
            'amount' => ['value' => '100.00', 'currency' => 'EUR'],
            'account_country' => 'DE',
            'transaction' => [
                'product_id' => 'other_commercial',
                'variant_id' => 'standard',
                'transaction_region' => 'international',
                'payer_region' => 'US',
            ],
        ]);

        $this->assertSame('4.34', $result['processing_fee']['value']);
        $this->assertSame('95.66', $result['net_amount']['value']);
        $this->assertCount(2, $result['components']);
        $this->assertSame('surcharge', $result['components'][1]['type']);
    }

    public function testPayPalQuoteMissingPayerRegionThrows(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(paypal: $this->paypalCore());

        $this->expectException(InsufficientTransactionContext::class);
        $engine->quote([
            'provider' => 'paypal',
            'amount' => ['value' => '100.00', 'currency' => 'EUR'],
            'account_country' => 'DE',
            'transaction' => [
                'product_id' => 'other_commercial',
                'variant_id' => 'standard',
                'transaction_region' => 'international',
            ],
        ]);
    }

    public function testStripeQuoteBase(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(stripe: $this->stripeCore());
        $result = $engine->quote([
            'provider' => 'stripe',
            'amount' => ['value' => '100.00', 'currency' => 'USD'],
            'account_country' => 'US',
            'transaction' => [
                'product_id' => 'payment',
                'variant_id' => 'card',
                'payment_method' => 'card',
                'channel' => 'online',
                'card' => ['region' => 'domestic', 'origin' => 'domestic'],
            ],
        ]);

        $this->assertSame('stripe', $result['provider']);
        $this->assertSame('3.20', $result['processing_fee']['value']);
        $this->assertSame('USD', $result['processing_fee']['currency']);
        $this->assertSame('96.80', $result['net_amount']['value']);
        $this->assertSame('exact_for_public_rate', $result['status']);
    }

    public function testStripeQuoteWithAdditive(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(stripe: $this->stripeCore());
        $result = $engine->quote([
            'provider' => 'stripe',
            'amount' => ['value' => '100.00', 'currency' => 'USD'],
            'account_country' => 'US',
            'transaction' => [
                'product_id' => 'payment',
                'variant_id' => 'card',
                'payment_method' => 'card',
                'channel' => 'online',
                'card' => ['region' => 'domestic', 'origin' => 'domestic', 'entry_mode' => 'manual'],
            ],
        ]);

        $this->assertSame('3.70', $result['processing_fee']['value']);
        $this->assertSame('96.30', $result['net_amount']['value']);
        $this->assertCount(2, $result['components']);
        $this->assertSame('surcharge', $result['components'][1]['type']);
    }

    public function testUnknownProviderThrows(): void
    {
        $engine = new PaymentFeeEngine();

        $this->expectException(UnknownProvider::class);
        $engine->quote([
            'provider' => 'braintree',
            'amount' => ['value' => '100.00', 'currency' => 'USD'],
            'account_country' => 'US',
            'transaction' => ['product_id' => 'payment', 'variant_id' => 'card'],
        ]);
    }

    public function testQuoteMissingMarketThrows(): void
    {
        $engine = PaymentFeeEngine::fromDocuments(stripe: $this->stripeCore());

        $this->expectException(QuoteNotAvailable::class);
        $engine->quote([
            'provider' => 'stripe',
            'amount' => ['value' => '100.00', 'currency' => 'USD'],
            'account_country' => 'CA',
            'transaction' => [
                'product_id' => 'payment',
                'variant_id' => 'card',
                'payment_method' => 'card',
            ],
        ]);
    }
}
