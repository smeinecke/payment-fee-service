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

echo json_encode($engine->auditContract()), PHP_EOL;
