<?php

declare(strict_types=1);

namespace Smeinecke\PaymentFee\Model;

final readonly class CardContext
{
    public ?string $origin;
    public ?string $region;
    public ?string $type;
    public ?string $network;
    public ?string $tier;
    public ?string $entryMode;

    public function __construct(
        ?string $origin = null,
        ?string $region = null,
        ?string $type = null,
        ?string $network = null,
        ?string $tier = null,
        ?string $entryMode = null,
    ) {
        $this->origin = $origin;
        $this->region = $region;
        $this->type = $type;
        $this->network = $network;
        $this->tier = $tier;
        $this->entryMode = $entryMode;
    }
}
