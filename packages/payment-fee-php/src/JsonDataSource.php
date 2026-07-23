<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

final class DataLocation
{
    public function __construct(
        public readonly string $provider,
        public readonly ?string $localPath = null,
        public readonly ?string $baseUrl = null,
        public readonly ?string $dataRef = null,
    ) {}
}

final class JsonDataSource
{
    private const DEFAULT_PAYPAL_URL = 'https://raw.githubusercontent.com/smeinecke/paypal-fee-data/{data_ref}';
    private const DEFAULT_STRIPE_URL = 'https://raw.githubusercontent.com/smeinecke/stripe-fee-data/{data_ref}';

    public readonly string $cacheDir;

    public function __construct(
        public readonly DataLocation $location,
        ?string $cacheDir = null,
        public readonly float $ttlSeconds = 86400.0,
        public readonly bool $autoRefresh = true,
        public readonly float $timeoutSeconds = 30.0,
    ) {
        $this->cacheDir = $cacheDir ?? self::defaultCacheDir();
    }

    public static function defaultCacheDir(): string
    {
        $home = getenv('HOME');
        if ($home !== false && $home !== '') {
            return $home . '/.cache/payment-fee';
        }

        return sys_get_temp_dir() . '/payment-fee';
    }

    public static function defaultPayPalUrl(): string
    {
        return self::DEFAULT_PAYPAL_URL;
    }

    public static function defaultStripeUrl(): string
    {
        return self::DEFAULT_STRIPE_URL;
    }

    public function readText(string $relativePath): string
    {
        if ($this->location->localPath !== null) {
            $path = $this->location->localPath . '/' . ltrim($relativePath, '/');
            $contents = @file_get_contents($path);
            if ($contents === false) {
                throw new \RuntimeException("Unable to read {$path}");
            }

            return $contents;
        }

        if ($this->location->baseUrl === null) {
            throw new \RuntimeException("No data source configured for {$this->location->provider}");
        }

        return $this->readRemote($relativePath);
    }

    /**
     * @return array<string, mixed>
     */
    public function readJson(string $relativePath): array
    {
        $text = $this->readText($relativePath);
        $decoded = json_decode($text, true);
        if (!\is_array($decoded)) {
            throw new \RuntimeException("Invalid JSON in {$relativePath}");
        }

        /** @var array<string, mixed> $decoded */
        return $decoded;
    }

    public function refresh(string $relativePath): string
    {
        $cachePath = $this->cachePath($relativePath);
        if (file_exists($cachePath)) {
            @unlink($cachePath);
        }

        return $this->readRemote($relativePath, true);
    }

    private function baseUrl(): string
    {
        $base = $this->location->baseUrl ?? '';
        if (str_contains($base, '{data_ref}')) {
            return str_replace('{data_ref}', $this->location->dataRef ?? 'main', $base);
        }

        return $base;
    }

    private function cachePath(string $relativePath): string
    {
        $ref = $this->location->dataRef ?? 'default';
        $file = $this->cacheDir . '/' . $this->location->provider . '/' . $ref . '/' . ltrim($relativePath, '/');
        $dir = \dirname($file);
        if (!is_dir($dir)) {
            mkdir($dir, 0o755, true);
        }

        return $file;
    }

    private function isFresh(string $cachePath): bool
    {
        $mtime = @filemtime($cachePath);
        if ($mtime === false) {
            return false;
        }

        return (time() - $mtime) < $this->ttlSeconds;
    }

    private function readRemote(string $relativePath, bool $force = false): string
    {
        $cachePath = $this->cachePath($relativePath);
        $hasCache = file_exists($cachePath);

        if ($hasCache && ($this->isFresh($cachePath) || (!$force && !$this->autoRefresh))) {
            $contents = @file_get_contents($cachePath);
            if ($contents !== false) {
                return $contents;
            }
        }

        $url = rtrim($this->baseUrl(), '/') . '/' . ltrim($relativePath, '/');
        try {
            $contents = $this->fetchUrl($url);
            file_put_contents($cachePath, $contents);

            return $contents;
        } catch (\Throwable $e) {
            if ($hasCache) {
                $stale = @file_get_contents($cachePath);
                if ($stale !== false) {
                    return $stale;
                }
            }

            throw new \RuntimeException("Failed to download {$url} for {$this->location->provider}: " . $e->getMessage(), 0, $e);
        }
    }

    private function fetchUrl(string $url): string
    {
        $context = stream_context_create([
            'http' => [
                'timeout' => $this->timeoutSeconds,
                'header' => "User-Agent: payment-fee-php\r\n",
            ],
        ]);
        $result = @file_get_contents($url, false, $context);
        if ($result !== false) {
            return $result;
        }

        if (\extension_loaded('curl')) {
            $ch = curl_init($url);
            if ($ch !== false) {
                curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
                curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
                curl_setopt($ch, CURLOPT_TIMEOUT, (int) $this->timeoutSeconds);
                curl_setopt($ch, CURLOPT_USERAGENT, 'payment-fee-php');
                $result = curl_exec($ch);
                $error = curl_error($ch);
                curl_close($ch);
                if (\is_string($result) && $result !== '') {
                    return $result;
                }
                if ($error !== '') {
                    throw new \RuntimeException($error);
                }
            }
        }

        throw new \RuntimeException('Unable to fetch remote URL');
    }
}

function dataLocationFromString(string $provider, string $value, ?string $dataRef = null): DataLocation
{
    if (str_starts_with($value, 'http://') || str_starts_with($value, 'https://')) {
        return new DataLocation($provider, null, $value, $dataRef ?? 'main');
    }

    return new DataLocation($provider, $value, null, $dataRef ?? 'local');
}
