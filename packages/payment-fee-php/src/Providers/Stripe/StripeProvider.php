<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers\Stripe;

use Smeinecke\PaymentFee\Exception\InsufficientTransactionContext;
use Smeinecke\PaymentFee\Exception\QuoteNotAvailable;
use Smeinecke\PaymentFee\Exception\UnknownMarket;
use Smeinecke\PaymentFee\Model\QuoteRequest;
use Smeinecke\PaymentFee\Model\StripeQuoteRequest;
use Smeinecke\PaymentFee\Providers\ProviderInterface;

final class StripeProvider implements ProviderInterface
{
    private array $core;

    /**
     * @param array<string, mixed> $core
     */
    public function __construct(array $core)
    {
        $this->core = $core;
    }

    /**
     * @return list<array<string, mixed>>
     */
    public function compileRules(QuoteRequest $request): array
    {
        $stripeRequest = $request;
        \assert($stripeRequest instanceof StripeQuoteRequest);

        $market = $this->findMarket($stripeRequest->accountCountry);
        $context = $this->buildContext($stripeRequest);
        $currency = $stripeRequest->amount->currency;

        $fullMatches = [];
        $missingMatches = [];

        foreach ($market['rules'] ?? [] as $rule) {
            $conditions = ConditionMatcher::normalizeConditions($rule);
            $conflict = false;
            $missing = [];
            foreach ($conditions as $condition) {
                $status = ConditionMatcher::conditionStatus($condition, $context);
                if ($status === 'conflict') {
                    $conflict = true;
                    break;
                }
                if ($status === 'missing') {
                    $missing[] = ConditionMatcher::apiFieldName($condition['dimension']);
                }
            }
            if ($conflict) {
                continue;
            }
            $specificity = \count($conditions);
            if ($missing !== []) {
                $missingMatches[] = ['rule' => $rule, 'missing' => array_values(array_unique($missing)), 'specificity' => $specificity];
            } else {
                $fullMatches[] = ['rule' => $rule, 'specificity' => $specificity];
            }
        }

        if ($fullMatches === []) {
            if ($missingMatches !== []) {
                $allMissing = [];
                foreach ($missingMatches as $m) {
                    foreach ($m['missing'] as $field) {
                        $allMissing[$field] = true;
                    }
                }
                throw new InsufficientTransactionContext(array_keys($allMissing), ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
            }
            throw new QuoteNotAvailable('No Stripe fee rule matched the supplied context.', ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
        }

        $specificities = array_column($fullMatches, 'specificity');
        $maxSpec = max($specificities);
        $mostSpecific = array_filter($fullMatches, fn($m) => $m['specificity'] === $maxSpec);

        if (!ConditionMatcher::anyEvaluable(array_column($mostSpecific, 'rule'))) {
            throw new QuoteNotAvailable('The most specific matching Stripe fee rule cannot be quoted.', [
                'provider' => 'stripe',
                'market' => $stripeRequest->accountCountry,
                'rule_ids' => array_map(fn($m) => (string) $m['rule']['rule_id'], $mostSpecific),
            ]);
        }

        $baseCandidates = [];
        foreach (ConditionMatcher::sortBySpecificityDesc($fullMatches) as $match) {
            $rule = $match['rule'];
            if (ConditionMatcher::isEvaluable($rule) && ($rule['behavior'] ?? 'base') !== 'additive') {
                $baseCandidates[] = $rule;
            }
        }

        if ($baseCandidates === []) {
            throw new QuoteNotAvailable('No evaluable base Stripe fee rule matched.', ['provider' => 'stripe', 'market' => $stripeRequest->accountCountry]);
        }

        $base = $baseCandidates[0];
        $additiveRules = ConditionMatcher::selectAdditiveRules($market['rules'] ?? [], $context);

        $rules = [ConditionMatcher::executableFromRule($base, $currency)];
        foreach ($additiveRules as $rule) {
            $rules[] = ConditionMatcher::executableFromRule($rule, $currency);
        }

        return array_map(fn($rule) => $rule->toArray(), $rules);
    }

    /**
     * @return array<string, int>
     */
    public function auditContract(): array
    {
        $total = 0;
        $parsed = 0;
        $skipped = 0;
        $contextRequired = 0;

        foreach ($this->core['markets'] ?? [] as $market) {
            foreach ($market['rules'] ?? [] as $rule) {
                $total += 1;
                if (!ConditionMatcher::isEvaluable($rule)) {
                    $skipped += 1;
                    continue;
                }
                $hasComponents = ($rule['fee_components'] ?? []) !== [];
                $hasLegacy = ($rule['basis_points'] ?? null) !== null || ($rule['fixed_amount'] ?? null) !== null;
                if (!$hasComponents && !$hasLegacy) {
                    $skipped += 1;
                    continue;
                }
                if (($rule['conditions'] ?? []) !== []) {
                    $contextRequired += 1;
                }
                $parsed += 1;
            }
        }

        return [
            'stripe_calculable_rules_total' => $total,
            'stripe_calculable_rules_parsed' => $parsed,
            'stripe_calculable_rules_skipped' => $skipped,
            'stripe_context_required' => $contextRequired,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function findMarket(string $code): array
    {
        foreach ($this->core['markets'] ?? [] as $market) {
            if (strcasecmp((string) ($market['account_country'] ?? ''), $code) === 0) {
                return $market;
            }
        }
        throw new UnknownMarket('stripe', $code);
    }

    /**
     * @return array<string, mixed>
     */
    private function buildContext(StripeQuoteRequest $request): array
    {
        $t = $request->transaction;
        $context = [
            'account_country' => strtoupper($request->accountCountry),
            'customer_country' => $request->customerCountry !== null ? strtoupper($request->customerCountry) : null,
            'amount_currency' => strtoupper($request->amount->currency),
            'transaction_amount' => $request->amount->value,
            'presentment_currency' => strtoupper($request->amount->currency),
            'settlement_currency' => $request->settlementCurrency !== null ? strtoupper($request->settlementCurrency) : null,
            'product_id' => $t->productId,
            'variant_id' => $t->variantId,
            'payment_method' => $t->paymentMethod,
            'payment_method_variant' => $t->paymentMethodVariant,
            'channel' => $t->channel,
            'pricing_plan' => $t->pricingPlan,
            'pricing_tier' => $t->pricingTier,
            'payer' => $t->payer,
            'unit' => $t->unit ?? 'per_transaction',
            'currency_conversion_required' => $t->currencyConversionRequired,
            'recurring' => $t->recurring,
            'billing_type' => $t->billingType,
            'transaction_region' => $t->transactionRegion,
            'cross_border' => $t->crossBorder,
            'integration_type' => $t->integrationType,
            'product_feature' => $t->productFeature,
            'contract_length' => $t->contractLength,
            'feature_enabled' => $t->featureEnabled,
            'dispute_state' => $t->disputeState,
            'success' => true,
        ];

        if ($t->card !== null) {
            $context['card_origin'] = $t->card->origin;
            $context['card_region'] = $t->card->region;
            $context['card_type'] = $t->card->type;
            $context['card_network'] = $t->card->network;
            $context['card_tier'] = $t->card->tier;
            $context['card_entry_mode'] = $t->card->entryMode;
        }

        if ($t->settlement !== null) {
            if ($t->settlement->currency !== null) {
                $context['settlement_currency'] = strtoupper($t->settlement->currency);
            }
            $context['settlement_timing'] = $t->settlement->timing;
        }

        if ($t->bank !== null) {
            $context['bank_account_validation'] = $t->bank->validation;
            $context['bank_transfer_type'] = $t->bank->transferType;
        }

        if ($t->context !== null && \array_key_exists('success', $t->context)) {
            $context['success'] = $t->context['success'];
        }

        foreach ($t->context ?? [] as $key => $value) {
            if (($context[$key] ?? null) === null) {
                $context[$key] = $value;
            }
        }

        return $context;
    }
}
