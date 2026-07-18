<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\Model\PayPalQuoteRequest;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Providers\PayPal\PayPalProvider;

final class PaymentFeeEngine
{
    /**
     * @var array<string, PayPalProvider>
     */
    private array $providers = [];

    public static function fromPaths(?string $paypal = null, ?string $stripe = null, bool $validate = false): self
    {
        $engine = new self();
        if ($paypal !== null) {
            $core = json_decode(file_get_contents($paypal . '/json/core-fees.json'), true);
            $engine->register('paypal', new PayPalProvider($core));
        }
        if ($stripe !== null) {
            // TODO: load Stripe provider from filesystem
        }
        return $engine;
    }

    /**
     * @param array<string, mixed>|null $paypal
     * @param array<string, mixed>|null $stripe
     */
    public static function fromDocuments(?array $paypal = null, ?array $stripe = null, bool $validate = false): self
    {
        $engine = new self();
        if ($paypal !== null) {
            $core = $paypal['core'] ?? $paypal;
            $engine->register('paypal', new PayPalProvider($core));
        }
        if ($stripe !== null) {
            // TODO: load Stripe provider from documents
        }
        return $engine;
    }

    public function register(string $provider, PayPalProvider $instance): void
    {
        $this->providers[$provider] = $instance;
    }

    public function quote(QuoteRequest $request): array
    {
        if (!isset($this->providers[$request->provider])) {
            throw new UnknownProvider($request->provider);
        }

        if ($request instanceof PayPalQuoteRequest) {
            $provider = $this->providers[$request->provider];
            $rules = $provider->compileRules($request);
            $calculator = new Calculator();
            $result = $calculator->calculate($request->amount, $request->amount->currency, $rules);

            return [
                'provider' => $request->provider,
                'status' => 'exact_for_public_rate',
                'amount' => $result['amount'],
                'processing_fee' => $result['processing_fee'],
                'net_amount' => $result['net_amount'],
                'components' => $result['components'],
                'matched_rules' => $result['matched_rules'],
                'selected_product_id' => $request->transaction->productId,
                'selected_variant_id' => $request->transaction->variantId,
                'assumptions' => [
                    'Public standard pricing was used; negotiated merchant pricing is not represented.',
                    'The published dataset does not encode provider settlement rounding, so standard currency rounding is used.',
                ],
                'warnings' => [],
                'data' => [
                    'provider' => $request->provider,
                    'schema_version' => 1,
                    'market' => $request->accountCountry,
                    'content_sha256' => null,
                    'source_urls' => [],
                    'source_updated_at' => null,
                    'data_ref' => 'documents',
                ],
            ];
        }

        throw new \RuntimeException('Stripe provider is not yet implemented.');
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
