<?php

declare(strict_types=1);

require __DIR__ . '/../packages/payment-fee-php/vendor/autoload.php';

use Smeinecke\PaymentFee\PaymentFeeEngine;

$input = json_decode((string) file_get_contents('php://stdin'), true);
if (!is_array($input)) {
    fwrite(STDERR, "Invalid JSON input.\n");
    exit(1);
}

$engine = PaymentFeeEngine::fromDocuments(
    $input['paypal'] ?? null,
    $input['stripe'] ?? null,
);

$counters = $engine->auditContract();

$output = [
    'counters' => $counters,
    'failures' => [],
];

echo json_encode($output), PHP_EOL;
