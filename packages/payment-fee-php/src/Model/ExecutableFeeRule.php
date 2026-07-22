<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

/**
 * Typed intermediate representation of a fee rule that is ready for calculation.
 *
 * Mirrors the shared ExecutableRule/ExecutableFeeRule shapes used by the
 * TypeScript and Python implementations.
 */
final readonly class ExecutableFeeRule
{
    public function __construct(
        public string $rule_id,
        public string $label,
        public string $component_type = 'processing',
        public string $behavior = 'base',
        public ?string $percentage = null,
        public ?string $basis_points = null,
        public ?string $fixed_amount = null,
        public ?string $fixed_currency = null,
        public ?string $minimum_amount = null,
        public ?string $maximum_amount = null,
        public string $classification_status = 'unclassified',
        public float|int $confidence = 0.0,
        public string $exactness = 'exact',
        public ?string $source_url = null,
        public ?string $payer = null,
        public ?string $unit = null,
    ) {}

    /**
     * @return array<string, mixed>
     */
    public function toArray(): array
    {
        return [
            'rule_id' => $this->rule_id,
            'label' => $this->label,
            'component_type' => $this->component_type,
            'behavior' => $this->behavior,
            'percentage' => $this->percentage,
            'basis_points' => $this->basis_points,
            'fixed_amount' => $this->fixed_amount,
            'fixed_currency' => $this->fixed_currency,
            'minimum_amount' => $this->minimum_amount,
            'maximum_amount' => $this->maximum_amount,
            'classification_status' => $this->classification_status,
            'confidence' => $this->confidence,
            'exactness' => $this->exactness,
            'source_url' => $this->source_url,
            'payer' => $this->payer,
            'unit' => $this->unit,
        ];
    }
}
