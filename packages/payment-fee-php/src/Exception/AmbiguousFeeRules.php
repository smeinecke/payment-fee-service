<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Exception;

final class AmbiguousFeeRules extends PaymentFeeException
{
    /**
     * @param list<string> $ruleIds
     */
    public function __construct(array $ruleIds, array $details = [])
    {
        parent::__construct(
            'AMBIGUOUS_FEE_RULES',
            'Multiple equally specific fee rules matched with different fee values.',
            ['candidate_rule_ids' => array_values(array_unique($ruleIds)), ...$details],
        );
    }
}
