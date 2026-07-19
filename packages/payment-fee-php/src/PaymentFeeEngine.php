<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee;

use Smeinecke\PaymentFee\Exception\UnknownProvider;
use Smeinecke\PaymentFee\Model\PayPalQuoteRequest;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Model\StripeQuoteRequest;
use Smeinecke\PaymentFee\Providers\PayPal\PayPalProvider;
use Smeinecke\PaymentFee\Providers\ProviderInterface;
use Smeinecke\PaymentFee\Providers\Stripe\StripeProvider;

final class PaymentFeeEngine
{
    /**
     * @var array<string, ProviderInterface>
     */
    private array $providers = [];

    public static function fromPaths(?string $paypal = null, ?string $stripe = null, bool $validate = false): self
    {
        $engine = new self();
        if ($paypal !== null) {
            $contents = file_get_contents($paypal . '/json/core-fees.json');
            if ($contents === false) {
                throw new \RuntimeException('Unable to read PayPal core-fees.json');
            }
            $core = json_decode($contents, true);
            $engine->register('paypal', new PayPalProvider($core));
        }
        if ($stripe !== null) {
            $contents = file_get_contents($stripe . '/json/core-fees.json');
            if ($contents === false) {
                throw new \RuntimeException('Unable to read Stripe core-fees.json');
            }
            $core = json_decode($contents, true);
            $engine->register('stripe', new StripeProvider($core));
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
            $core = $stripe['core'] ?? $stripe;
            $engine->register('stripe', new StripeProvider($core));
        }
        return $engine;
    }

    public function register(string $provider, ProviderInterface $instance): void
    {
        $this->providers[$provider] = $instance;
    }

    public function quote(QuoteRequest|array $request): array
    {
        if (\is_array($request)) {
            $request = QuoteRequestFactory::fromArray($request);
        }
        if (!isset($this->providers[$request->provider])) {
            throw new UnknownProvider($request->provider);
        }

        if ($request instanceof PayPalQuoteRequest) {
            $provider = $this->providers[$request->provider];
            $rules = $provider->compileRules($request);
            $calculator = new Calculator();
            $result = $calculator->calculate($request->amount, $request->amount->currency, $rules);
            $status = $this->deriveStatus($rules);

            return [
                'provider' => $request->provider,
                'status' => $status,
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

        if ($request instanceof StripeQuoteRequest) {
            $provider = $this->providers[$request->provider];
            $rules = $provider->compileRules($request);
            $calculator = new Calculator();
            $result = $calculator->calculate($request->amount, $request->amount->currency, $rules);
            $status = $this->deriveStatus($rules);

            return [
                'provider' => $request->provider,
                'status' => $status,
                'amount' => $result['amount'],
                'processing_fee' => $result['processing_fee'],
                'net_amount' => $result['net_amount'],
                'components' => $result['components'],
                'matched_rules' => $result['matched_rules'],
                'selected_product_id' => $request->transaction->productId,
                'selected_variant_id' => $request->transaction->variantId,
                'assumptions' => [
                    'Public standard pricing was used; negotiated or IC++ pricing is not represented.',
                    'The published dataset does not encode provider settlement rounding, so standard currency rounding is used.',
                    'Assumed a successful transaction for providers that require success.',
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

        throw new UnknownProvider($request->provider);
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
        $result = [];
        foreach ($this->providers as $provider) {
            $audit = $provider->auditContract();
            foreach ($audit as $key => $value) {
                $result[$key] = ($result[$key] ?? 0) + $value;
            }
        }
        $required = [
            'paypal_calculable_rules_total',
            'paypal_calculable_rules_parsed',
            'paypal_calculable_rules_skipped',
            'paypal_context_required',
            'stripe_calculable_rules_total',
            'stripe_calculable_rules_parsed',
            'stripe_calculable_rules_skipped',
            'stripe_context_required',
            'unknown_fields',
            'unknown_condition_dimensions',
            'unknown_condition_operators',
            'unsupported_fee_components',
            'unresolved_schedule_references',
        ];
        foreach ($required as $key) {
            if (!isset($result[$key])) {
                $result[$key] = 0;
            }
        }
        return $result;
    }

    /**
     * @param list<array<string, mixed>> $rules
     */
    private function deriveStatus(array $rules): string
    {
        $nonAdditive = array_filter($rules, fn($r) => ($r['behavior'] ?? 'base') !== 'additive');
        if (
            $nonAdditive !== [] &&
            array_reduce(
                array_values($nonAdditive),
                fn($carry, $r) => $carry && \in_array($r['behavior'] ?? 'base', ['free', 'included', 'waived'], true),
                true,
            )
        ) {
            return 'included';
        }
        foreach ($rules as $rule) {
            if (\in_array($rule['exactness'] ?? '', ['range', 'from', 'up_to'], true)) {
                return 'range';
            }
        }
        foreach ($rules as $rule) {
            $exactness = $rule['exactness'] ?? '';
            if ($exactness !== '' && !\in_array($exactness, ['exact', 'exact_for_public_rate'], true)) {
                return 'estimated';
            }
            $status = $rule['classification_status'] ?? '';
            if ($status !== '' && !\in_array($status, ['calculable', 'calculable_rule', 'exact', 'exact_for_public_rate'], true)) {
                return 'estimated';
            }
        }
        return 'exact_for_public_rate';
    }

    private function requireProvider(string $provider): void
    {
        if (!isset($this->providers[$provider])) {
            throw new UnknownProvider($provider);
        }
    }
}
