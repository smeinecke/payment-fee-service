<?php

declare(strict_types=1);

require __DIR__ . '/../../packages/payment-fee-php/vendor/autoload.php';

use Smeinecke\PaymentFee\PaymentFeeEngine;

function stripNulls(mixed $value): mixed
{
    if (is_array($value)) {
        $out = [];
        foreach ($value as $k => $v) {
            if ($v !== null) {
                $out[$k] = stripNulls($v);
            }
        }
        return $out;
    }
    return $value;
}

function canonical(mixed $value): mixed
{
    if (is_array($value)) {
        if (array_keys($value) === range(0, count($value) - 1)) {
            return array_map('canonical', $value);
        }
        $sorted = [];
        $keys = array_keys($value);
        sort($keys);
        foreach ($keys as $key) {
            $sorted[$key] = canonical($value[$key]);
        }
        return $sorted;
    }
    return $value;
}

function deepEqual(mixed $a, mixed $b): bool
{
    return json_encode(canonical($a)) === json_encode(canonical($b));
}

function normalize(mixed $result): mixed
{
    return $result === null ? null : stripNulls($result);
}

function runCase(array $case): array
{
    $providerDocuments = $case['provider_documents'] ?? [];
    $actual = null;
    $actualError = null;
    try {
        $engine = PaymentFeeEngine::fromDocuments(
            $providerDocuments['paypal'] ?? null,
            $providerDocuments['stripe'] ?? null,
        );
        $actual = $engine->quote($case['request']);
    } catch (Throwable $e) {
        $actualError = [
            'code' => $e instanceof \Smeinecke\PaymentFee\Exception\PaymentFeeException ? $e->getErrorCode() : null,
            'message' => $e->getMessage(),
            'details' => $e instanceof \Smeinecke\PaymentFee\Exception\PaymentFeeException ? $e->getDetails() : [],
        ];
    }

    $expected = $case['expected_result'] ?? null;
    $expectedError = $case['expected_error'] ?? null;

    $status = 'ok';
    if (!deepEqual(normalize($actual), normalize($expected))) {
        $status = 'mismatch';
    }
    if ($status === 'ok' && !deepEqual(normalize($actualError), normalize($expectedError))) {
        $status = 'mismatch';
    }

    return [
        'id' => $case['id'],
        'status' => $status,
        'actual' => $actual,
        'expected' => $expected,
        'actual_error' => $actualError,
        'expected_error' => $expectedError,
    ];
}

function main(): int
{
    $emitPath = null;
    foreach ($GLOBALS['argv'] ?? [] as $i => $arg) {
        if ($arg === '--emit' && isset($GLOBALS['argv'][$i + 1])) {
            $emitPath = $GLOBALS['argv'][$i + 1];
            break;
        }
    }

    $manifest = json_decode(file_get_contents(__DIR__ . '/../../contracts/conformance/manifest.json'), true);
    $failures = [];
    $emitted = [];
    foreach ($manifest['cases'] as $casePath) {
        $case = json_decode(file_get_contents(__DIR__ . '/../../contracts/conformance/' . $casePath), true);
        $result = runCase($case);
        echo "{$result['id']}: {$result['status']}" . PHP_EOL;
        $emitted[] = [
            'id' => $result['id'],
            'status' => $result['status'],
            'actual' => $result['actual'],
            'error' => $result['actual_error'],
        ];
        if ($result['status'] !== 'ok') {
            $failures[] = [
                'id' => $result['id'],
                'status' => $result['status'],
                'field' => !deepEqual(normalize($result['actual']), normalize($result['expected'])) ? 'result' : 'error',
                'actual' => $result['actual'],
                'expected' => $result['expected'],
            ];
        }
    }

    if ($emitPath !== null) {
        file_put_contents($emitPath, json_encode($emitted, JSON_PRETTY_PRINT));
    }

    if ($failures) {
        fwrite(STDERR, "\nFailures:" . PHP_EOL);
        foreach ($failures as $failure) {
            fwrite(STDERR, json_encode($failure, JSON_PRETTY_PRINT) . PHP_EOL);
        }
        return 1;
    }

    echo "\nAll PHP conformance cases passed." . PHP_EOL;
    return 0;
}

exit(main());
