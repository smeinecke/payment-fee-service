<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\Model\QuoteRequest;

/**
 * Native PHP payment-fee calculation engine.
 *
 * @todo Implement PayPal and Stripe provider adapters.
 */
final class PaymentFeeEngine
{
    /**
     * @var array<string, object>
     */
    private array $providers = [];

    public static function fromPaths(?string $paypal = null, ?string $stripe = null, bool $validate = false): self
    {
        $engine = new self();
        if ($paypal !== null) {
            // TODO: load PayPal provider from filesystem
        }
        if ($stripe !== null) {
            // TODO: load Stripe provider from filesystem
        }
        return $engine;
    }

    /**
     * @param array<string, mixed> $paypal
     * @param array<string, mixed> $stripe
     */
    public static function fromDocuments(?array $paypal = null, ?array $stripe = null, bool $validate = false): self
    {
        $engine = new self();
        if ($paypal !== null) {
            // TODO: load PayPal provider from documents
        }
        if ($stripe !== null) {
            // TODO: load Stripe provider from documents
        }
        return $engine;
    }

    public function quote(QuoteRequest $request): array
    {
        if (!isset($this->providers[$request->provider])) {
            throw new UnknownProvider($request->provider);
        }
        // TODO: compile rules and calculate
        throw new \RuntimeException('Provider adapters are not yet implemented.');
    }

    /**
     * @return list<string>
     */
    public function providers(): array
    {
        return array_keys($this->providers);
    }

    /**
     * @return list<array<string, mixed>>
     */
    public function markets(string $provider): array
    {
        $this->requireProvider($provider);
        return [];
    }

    /**
     * @return array<string, mixed>
     */
    public function capabilities(string $provider, string $market): array
    {
        $this->requireProvider($provider);
        return [];
    }

    /**
     * @return array<string, mixed>
     */
    public function quoteSchema(string $provider, string $market): array
    {
        $this->requireProvider($provider);
        return [];
    }

    /**
     * @return list<array<string, mixed>>
     */
    public function dataStatus(): array
    {
        return [];
    }

    /**
     * @return array<string, int>
     */
    public function auditContract(): array
    {
        return [];
    }

    private function requireProvider(string $provider): void
    {
        if (!isset($this->providers[$provider])) {
            throw new UnknownProvider($provider);
        }
    }
}
