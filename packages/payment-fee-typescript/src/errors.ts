export type PaymentFeeErrorDetails = Record<string, unknown>;

export abstract class PaymentFeeError extends Error {
  abstract readonly code: string;
  readonly details: PaymentFeeErrorDetails;

  constructor(message: string, details: PaymentFeeErrorDetails = {}) {
    super(message);
    this.details = details;
  }
}

export class UnknownProvider extends PaymentFeeError {
  readonly code = "UNKNOWN_PROVIDER";
  constructor(provider: string, details: PaymentFeeErrorDetails = {}) {
    super(`Unknown provider: ${provider}`, { provider, ...details });
  }
}

export class UnknownMarket extends PaymentFeeError {
  readonly code = "UNKNOWN_MARKET";
  constructor(provider: string, market: string, details: PaymentFeeErrorDetails = {}) {
    super(`Provider ${provider} has no published market ${market}.`, {
      provider,
      market,
      ...details,
    });
  }
}

export class ProviderDataUnavailable extends PaymentFeeError {
  readonly code = "PROVIDER_DATA_UNAVAILABLE";
  constructor(provider: string, reason: string, details: PaymentFeeErrorDetails = {}) {
    super(`Validated data for ${provider} is unavailable.`, {
      provider,
      reason,
      ...details,
    });
  }
}

export class QuoteNotAvailable extends PaymentFeeError {
  readonly code = "QUOTE_NOT_AVAILABLE";
  constructor(message: string, details: PaymentFeeErrorDetails = {}) {
    super(message, details);
  }
}

export class InsufficientTransactionContext extends PaymentFeeError {
  readonly code = "INSUFFICIENT_TRANSACTION_CONTEXT";
  constructor(missingFields: string[], details: PaymentFeeErrorDetails = {}) {
    super("Additional transaction context is required to select an applicable fee rule.", {
      missing_fields: [...new Set(missingFields)].sort(),
      ...details,
    });
  }
}

export class AmbiguousFeeRules extends PaymentFeeError {
  readonly code = "AMBIGUOUS_FEE_RULES";
  constructor(ruleIds: string[], details: PaymentFeeErrorDetails = {}) {
    super("Multiple equally specific fee rules matched with different fee values.", {
      candidate_rule_ids: [...new Set(ruleIds)].sort(),
      ...details,
    });
  }
}

export class UnsupportedFeeShape extends PaymentFeeError {
  readonly code = "UNSUPPORTED_FEE_SHAPE";
  constructor(message: string, details: PaymentFeeErrorDetails = {}) {
    super(message, details);
  }
}

export class CurrencyMismatch extends PaymentFeeError {
  readonly code = "CURRENCY_MISMATCH";
  constructor(message: string, details: PaymentFeeErrorDetails = {}) {
    super(message, details);
  }
}

export class DatasetValidationException extends PaymentFeeError {
  readonly code = "DATASET_VALIDATION_ERROR";
  constructor(message: string, details: PaymentFeeErrorDetails = {}) {
    super(message, details);
  }
}
