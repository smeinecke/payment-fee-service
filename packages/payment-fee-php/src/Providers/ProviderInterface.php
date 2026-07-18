<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Providers;

use Smeinecke\PaymentFee\Model\QuoteRequest;

interface ProviderInterface
{
    /**
     * @return list<array<string, mixed>>
     */
    public function compileRules(QuoteRequest $request): array;

    /**
     * @return array<string, int>
     */
    public function auditContract(): array;
}
