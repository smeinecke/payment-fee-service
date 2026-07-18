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

$engine = PaymentFeeEngine::fromDocuments([
    'core' => [
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
                            'calculation_status' => 'calculable',
                            'fee_components' => [
                                ['type' => 'percentage', 'value' => '2.49'],
                                ['type' => 'fixed_fee_schedule', 'schedule_id' => 'fixed__applies_to_markets=DE'],
                            ],
                        ],
                    ],
                    'fixed_fee_schedules' => [
                        'fixed__applies_to_markets=DE' => [
                            'entries' => ['EUR' => '0.35'],
                        ],
                    ],
                ],
            ],
        ],
    ],
]);

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

$expected = '2.84';
if ($result['processing_fee']['value'] !== $expected) {
    fwrite(STDERR, "Unexpected processing fee: {$result['processing_fee']['value']} (expected {$expected})\n");
    exit(1);
}

echo "PHP isolated install OK\n";
PHP
php smoke.php
