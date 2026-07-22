import { Decimal } from "decimal.js";
import { InsufficientTransactionContext, QuoteNotAvailable, UnknownMarket } from "../../errors.js";
import type { ExecutableRule } from "../../calculator.js";
import type { QuoteRequest, StripeQuoteRequest } from "../../models.js";
import type { StripeRule } from "./condition-matcher.js";
import {
  apiFieldName,
  conditionStatus,
  executableFromRule,
  isEvaluable,
  normalizeConditions,
  selectAdditiveRules,
  sortBySpecificityDesc,
} from "./condition-matcher.js";

export interface StripeMarket {
  account_country: string;
  rules?: StripeRule[];
}

export interface StripeCore {
  schema_version?: number;
  markets?: StripeMarket[];
}

export interface StripeIndexMarket {
  account_country: string;
  content_sha256?: string | null;
  source_urls?: string[];
  source_updated_at?: string | null;
}

export interface StripeIndex {
  schema_version?: number;
  markets?: StripeIndexMarket[];
}

export class StripeProvider {
  private readonly markets = new Map<string, StripeMarket>();

  constructor(private readonly core: StripeCore) {
    for (const market of core.markets ?? []) {
      this.markets.set(market.account_country.toUpperCase(), market);
    }
  }

  compileRules(request: QuoteRequest): ExecutableRule[] {
    const stripeRequest = request as StripeQuoteRequest;
    const market = this.markets.get(stripeRequest.account_country.toUpperCase());
    if (!market) {
      throw new UnknownMarket("stripe", stripeRequest.account_country);
    }

    const currency = stripeRequest.amount.currency;
    const context = buildContext(stripeRequest);

    const fullMatches: { rule: StripeRule; specificity: number }[] = [];
    const missingMatches: { rule: StripeRule; missing: string[]; specificity: number }[] = [];

    for (const rule of market.rules ?? []) {
      const conditions = normalizeConditions(rule);
      let conflict = false;
      const missing: string[] = [];
      for (const condition of conditions) {
        const status = conditionStatus(condition, context);
        if (status === "conflict") {
          conflict = true;
          break;
        }
        if (status === "missing") {
          missing.push(apiFieldName(condition.dimension));
        }
      }
      if (conflict) continue;
      const specificity = conditions.length;
      if (missing.length > 0) {
        missingMatches.push({ rule, missing: [...new Set(missing)].sort(), specificity });
      } else {
        fullMatches.push({ rule, specificity });
      }
    }

    if (fullMatches.length === 0) {
      if (missingMatches.length > 0) {
        const allMissing = [...new Set(missingMatches.flatMap((m) => m.missing))].sort();
        throw new InsufficientTransactionContext(allMissing, {
          provider: "stripe",
          market: stripeRequest.account_country,
        });
      }
      throw new QuoteNotAvailable("No Stripe fee rule matched the supplied context.", {
        provider: "stripe",
        market: stripeRequest.account_country,
      });
    }

    const maxSpec = Math.max(...fullMatches.map((m) => m.specificity));
    const mostSpecific = fullMatches.filter((m) => m.specificity === maxSpec).map((m) => m.rule);

    if (!mostSpecific.some((r) => isEvaluable(r))) {
      throw new QuoteNotAvailable("The most specific matching Stripe fee rule cannot be quoted.", {
        provider: "stripe",
        market: stripeRequest.account_country,
        rule_ids: mostSpecific.map((r) => r.rule_id),
      });
    }

    const baseCandidates = sortBySpecificityDesc(fullMatches)
      .map((m) => m.rule)
      .filter((r) => isEvaluable(r) && (r.behavior ?? "base") !== "additive");

    if (baseCandidates.length === 0) {
      throw new QuoteNotAvailable("No evaluable base Stripe fee rule matched.", {
        provider: "stripe",
        market: stripeRequest.account_country,
      });
    }

    const base = baseCandidates[0];
    const additiveRules = selectAdditiveRules(market.rules ?? [], context);

    const rules = [executableFromRule(base, currency)];
    for (const rule of additiveRules) {
      rules.push(executableFromRule(rule, currency));
    }

    return rules;
  }

  auditContract(): Record<string, number> {
    let total = 0;
    let parsed = 0;
    let skipped = 0;
    let contextRequired = 0;

    for (const market of this.core.markets ?? []) {
      for (const rule of market.rules ?? []) {
        total += 1;
        if (!isEvaluable(rule)) {
          skipped += 1;
          continue;
        }
        if ((rule.fee_components ?? []).length === 0 && !rule.basis_points && !rule.fixed_amount) {
          skipped += 1;
          continue;
        }
        if ((rule.conditions ?? []).length > 0) {
          contextRequired += 1;
        }
        parsed += 1;
      }
    }

    return {
      stripe_calculable_rules_total: total,
      stripe_calculable_rules_parsed: parsed,
      stripe_calculable_rules_skipped: skipped,
      stripe_context_required: contextRequired,
    };
  }
}

function isNumeric(value: unknown): boolean {
  return typeof value === "number" || (typeof value === "string" && !Number.isNaN(Number(value)));
}

function contextValuesEqual(a: unknown, b: unknown): boolean {
  if (typeof a === "boolean" || typeof b === "boolean") {
    return Boolean(a) === Boolean(b);
  }
  if (isNumeric(a) && isNumeric(b)) {
    try {
      return new Decimal(String(a)).eq(new Decimal(String(b)));
    } catch {
      return false;
    }
  }
  return a === b;
}

function buildContext(request: StripeQuoteRequest): Record<string, unknown> {
  const t = request.transaction;
  const context: Record<string, unknown> = {
    account_country: request.account_country.toUpperCase(),
    customer_country: request.customer_country?.toUpperCase() ?? null,
    amount_currency: request.amount.currency.toUpperCase(),
    transaction_amount: request.amount.value,
    presentment_currency: request.amount.currency.toUpperCase(),
    settlement_currency: request.settlement_currency?.toUpperCase() ?? null,
    product_id: t.product_id ?? null,
    variant_id: t.variant_id ?? null,
    payment_method: t.payment_method ?? null,
    payment_method_variant: t.payment_method_variant ?? null,
    channel: t.channel ?? null,
    pricing_plan: t.pricing_plan ?? null,
    pricing_tier: t.pricing_tier ?? null,
    payer: t.payer ?? null,
    unit: t.unit ?? "per_transaction",
    currency_conversion_required: t.currency_conversion_required ?? null,
    recurring: t.recurring ?? null,
    billing_type: t.billing_type ?? null,
    transaction_region: t.transaction_region ?? null,
    cross_border: t.cross_border ?? null,
    integration_type: t.integration_type ?? null,
    product_feature: t.product_feature ?? null,
    contract_length: t.contract_length ?? null,
    feature_enabled: t.feature_enabled ?? null,
    dispute_state: t.dispute_state ?? null,
    success: true,
  };

  if (t.card) {
    context.card_origin = t.card.origin ?? null;
    context.card_region = t.card.region ?? null;
    context.card_type = t.card.type ?? null;
    context.card_network = t.card.network ?? null;
    context.card_tier = t.card.tier ?? null;
    context.card_entry_mode = t.card.entry_mode ?? null;
  }

  if (t.settlement) {
    context.settlement_currency ??= t.settlement.currency?.toUpperCase();
    context.settlement_timing = t.settlement.timing ?? null;
  }

  if (t.bank) {
    context.bank_account_validation = t.bank.validation ?? null;
    context.bank_transfer_type = t.bank.transfer_type ?? null;
  }

  if (t.context?.success !== undefined) {
    context.success = t.context.success;
  }

  for (const [key, value] of Object.entries(t.context ?? {})) {
    if (value === undefined) {
      continue;
    }
    const existing = context[key];
    if (existing !== undefined && existing !== null) {
      if (!contextValuesEqual(value, existing)) {
        throw new QuoteNotAvailable("Contradictory duplicate value in transaction context.", {
          field: key,
          typed_value: existing,
          context_value: value,
        });
      }
    } else {
      context[key] = value;
    }
  }

  return context;
}
