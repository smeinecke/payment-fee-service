#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PHP_PACKAGE="$REPO_ROOT/packages/payment-fee-php"
TMP_DIR="$(mktemp -d)"
COMPOSER_BIN="${COMPOSER_BIN:-composer}"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$TMP_DIR"
cat > composer.json <<EOF
{
  "repositories": [
    {
      "type": "path",
      "url": "$PHP_PACKAGE"
    }
  ],
  "require": {
    "smeinecke/payment-fee": "*"
  },
  "minimum-stability": "dev"
}
EOF

"$COMPOSER_BIN" install --no-interaction --quiet

cat > smoke.php <<'PHP'
<?php
require 'vendor/autoload.php';

use Smeinecke\PaymentFee\PaymentFeeEngine;
use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;

$paypalCore = [
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
                            ['type' => 'percentage', 'value' => '2.49'],
                            ['type' => 'fixed_fee_schedule'],
                            ['type' => 'international_surcharge_schedule'],
                        ],
                    ],
                ],
                'fixed_fee_schedules' => [
                    'fixed__applies_to_markets=DE' => [
                        'entries' => ['EUR' => '0.35'],
                    ],
                ],
                'international_surcharge_schedules' => [
                    'surcharge__applies_to_markets=DE' => [
                        'entries' => [
                            ['payer_region' => 'US', 'percentage_points' => '1.5'],
                        ],
                    ],
                ],
            ],
        ],
    ],
];

$stripeCore = [
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

$engine = PaymentFeeEngine::fromDocuments($paypalCore, $stripeCore);

$paypalDomestic = $engine->quote([
    'provider' => 'paypal',
    'amount' => ['value' => '100.00', 'currency' => 'EUR'],
    'account_country' => 'DE',
    'transaction' => [
        'product_id' => 'other_commercial',
        'variant_id' => 'standard',
        'transaction_region' => 'domestic',
    ],
]);
assert($paypalDomestic['processing_fee']['value'] === '2.84', 'PayPal domestic fee mismatch');

$paypalInternational = $engine->quote([
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
assert($paypalInternational['processing_fee']['value'] === '4.34', 'PayPal international fee mismatch');

$threw = false;
try {
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
} catch (InsufficientTransactionContext $e) {
    $threw = true;
}
assert($threw, 'Expected InsufficientTransactionContext for missing payer region');

$stripeBase = $engine->quote([
    'provider' => 'stripe',
    'amount' => ['value' => '100.00', 'currency' => 'USD'],
    'account_country' => 'US',
    'transaction' => [
        'product_id' => 'payment',
        'variant_id' => 'card',
        'payment_method' => 'card',
        'channel' => 'online',
        'card' => ['origin' => 'domestic', 'region' => 'domestic'],
    ],
]);
assert($stripeBase['processing_fee']['value'] === '3.20', 'Stripe base fee mismatch');

$stripeAdditive = $engine->quote([
    'provider' => 'stripe',
    'amount' => ['value' => '100.00', 'currency' => 'USD'],
    'account_country' => 'US',
    'transaction' => [
        'product_id' => 'payment',
        'variant_id' => 'card',
        'payment_method' => 'card',
        'channel' => 'online',
        'card' => ['origin' => 'domestic', 'region' => 'domestic', 'entry_mode' => 'manual'],
    ],
]);
assert($stripeAdditive['processing_fee']['value'] === '3.70', 'Stripe additive fee mismatch');

echo "PHP isolated install OK\n";
PHP
php smoke.php
