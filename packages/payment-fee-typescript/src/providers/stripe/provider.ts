import { Decimal } from "decimal.js";
import {
  InsufficientTransactionContext,
  QuoteNotAvailable,
  UnsupportedFeeShape,
} from "../../errors.js";
import type { ExecutableRule } from "../../calculator.js";
import type { QuoteRequest, StripeQuoteRequest } from "../../models.js";
import { toDecimal } from "../../rounding.js";

export interface StripeFeeCondition {
  dimension: string;
  operator?: string;
  value: unknown;
}

export interface StripeFeeComponent {
  type: string;
  amount?: string | null;
  basis_points?: string | null;
  currency?: string | null;
  value?: string | null;
}

export interface StripeRule {
  rule_id: string;
  provider?: string;
  account_country?: string | null;
  behavior?: string;
  classification_status?: string;
  conditions?: StripeFeeCondition[];
  confidence?: number;
  exactness?: string;
  fee_components?: StripeFeeComponent[];
  label?: string | null;
  name?: string | null;
  payment_method?: string | null;
  product_id?: string | null;
  unit?: string;
  variant_id?: string | null;
  basis_points?: string | null;
  fixed_amount?: string | null;
  fixed_currency?: string | null;
  minimum_amount?: string | null;
  maximum_amount?: string | null;
  source_url?: string | null;
  payer?: string | null;
}

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

export interface StripePaymentMethods {
  schema_version?: number;
  methods?: unknown[];
}

const EVALUABLE_CLASSIFICATION_STATUSES = new Set(["calculable_rule", "free", "included"]);

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
      throw new QuoteNotAvailable("Stripe market not found.", {
        market: stripeRequest.account_country,
      });
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

  if (t.context?.success !== undefined && t.context.success !== null) {
    context.success = t.context.success;
  }

  for (const [key, value] of Object.entries(t.context ?? {})) {
    context[key] ??= value;
  }

  return context;
}

function normalizeConditions(
  rule: StripeRule,
): { dimension: string; operator: string; value: unknown }[] {
  const conditions: { dimension: string; operator: string; value: unknown }[] = [];
  const topLevel: [string, unknown][] = [
    ["account_country", rule.account_country],
    ["payment_method", rule.payment_method],
    ["payment_method_variant", null],
    ["product_id", rule.product_id],
    ["variant_id", rule.variant_id],
  ];
  for (const [dimension, value] of topLevel) {
    if (value !== undefined && value !== null) {
      conditions.push({ dimension, operator: "eq", value });
    }
  }

  for (const condition of rule.conditions ?? []) {
    conditions.push({
      dimension: condition.dimension,
      operator: (condition.operator ?? "eq").toLowerCase(),
      value: condition.value,
    });
  }

  return conditions;
}

function conditionStatus(
  condition: { dimension: string; operator: string; value: unknown },
  context: Record<string, unknown>,
): string {
  const actual = context[condition.dimension];
  const expected = condition.value;
  const operator = condition.operator;

  if (actual === undefined || actual === null) {
    return expected === undefined || expected === null ? "match" : "missing";
  }

  if (operator === "eq" || operator === "==" || operator === "equals") {
    if (Array.isArray(expected)) {
      return asList(expected).some((item) => valuesEqual(actual, item)) ? "match" : "conflict";
    }
    return valuesEqual(actual, expected) ? "match" : "conflict";
  }
  if (operator === "ne" || operator === "!=" || operator === "not_equals") {
    if (Array.isArray(expected)) {
      return asList(expected).every((item) => !valuesEqual(actual, item)) ? "match" : "conflict";
    }
    return !valuesEqual(actual, expected) ? "match" : "conflict";
  }
  if (operator === "in") {
    return asList(expected).some((item) => valuesEqual(actual, item)) ? "match" : "conflict";
  }
  if (operator === "not_in" || operator === "nin") {
    return asList(expected).every((item) => !valuesEqual(actual, item)) ? "match" : "conflict";
  }
  if (operator === "gt" || operator === "gte" || operator === "lt" || operator === "lte") {
    return numericCompare(toNumericString(actual), toNumericString(expected), operator)
      ? "match"
      : "conflict";
  }
  throw new UnsupportedFeeShape(`Unsupported condition operator: ${operator}`, { operator });
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (typeof left === "boolean" || typeof right === "boolean") {
    return Boolean(left) === Boolean(right);
  }
  if (typeof left === "string" && typeof right === "string") {
    return left.toLowerCase() === right.toLowerCase();
  }
  if (isNumeric(left) || isNumeric(right)) {
    try {
      return new Decimal(String(left)).eq(new Decimal(String(right)));
    } catch {
      return false;
    }
  }
  return left === right;
}

function isNumeric(value: unknown): boolean {
  return typeof value === "number" || typeof value === "string" || value instanceof Decimal;
}

function toNumericString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number") return value.toString();
  if (value instanceof Decimal) return value.toFixed();
  throw new UnsupportedFeeShape("Numeric condition contains a non-numeric value.", { value });
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [value];
}

function numericCompare(actual: string, expected: string, operator: string): boolean {
  const left = new Decimal(actual);
  const right = new Decimal(expected);
  switch (operator) {
    case "gt":
      return left.gt(right);
    case "gte":
      return left.gte(right);
    case "lt":
      return left.lt(right);
    case "lte":
      return left.lte(right);
    default:
      return false;
  }
}

function isEvaluable(rule: StripeRule): boolean {
  return EVALUABLE_CLASSIFICATION_STATUSES.has(rule.classification_status ?? "unclassified");
}

function sortBySpecificityDesc(
  matches: { rule: StripeRule; specificity: number }[],
): { rule: StripeRule; specificity: number }[] {
  return [...matches].sort((a, b) => b.specificity - a.specificity);
}

function selectAdditiveRules(rules: StripeRule[], context: Record<string, unknown>): StripeRule[] {
  const additive: StripeRule[] = [];
  for (const rule of rules) {
    if ((rule.behavior ?? "base") !== "additive") continue;
    const conditions = normalizeConditions(rule);
    if (conditions.some((c) => conditionStatus(c, context) !== "match")) continue;
    if (isEvaluable(rule)) {
      additive.push(rule);
    }
  }
  return additive;
}

function compileStripeComponents(
  rule: StripeRule,
  currency: string,
): {
  percentage: string | null;
  fixed_amount: string | null;
  minimum_amount: string | null;
  maximum_amount: string | null;
  behavior: string;
} {
  let basePercentage = new Decimal("0");
  let baseFixed = new Decimal("0");
  let additivePercentage = new Decimal("0");
  let additiveFixed = new Decimal("0");
  let minimumAmount: Decimal | null = null;
  let maximumAmount: Decimal | null = null;

  const components = [...(rule.fee_components ?? [])];
  if (components.length === 0 && rule.basis_points) {
    components.push({ type: "percentage", basis_points: rule.basis_points });
  }
  if (components.length === 0 && rule.fixed_amount) {
    components.push({
      type: "fixed_amount",
      amount: rule.fixed_amount,
      currency: rule.fixed_currency ?? currency,
    });
  }
  if (rule.minimum_amount) {
    components.push({
      type: "minimum_fee",
      amount: rule.minimum_amount,
      currency: rule.fixed_currency ?? currency,
    });
  }
  if (rule.maximum_amount) {
    components.push({
      type: "maximum_fee",
      amount: rule.maximum_amount,
      currency: rule.fixed_currency ?? currency,
    });
  }

  for (const comp of components) {
    const behavior = rule.behavior ?? "base";
    if (comp.type === "percentage") {
      const rate = componentRate(comp);
      if (behavior === "additive") {
        additivePercentage = additivePercentage.plus(rate);
      } else {
        basePercentage = basePercentage.plus(rate);
      }
    } else if (comp.type === "fixed_amount" || comp.type === "fixed_surcharge") {
      const fixed = componentFixed(comp, currency, rule.rule_id);
      if (behavior === "additive" || comp.type === "fixed_surcharge") {
        additiveFixed = additiveFixed.plus(fixed);
      } else {
        baseFixed = baseFixed.plus(fixed);
      }
    } else if (comp.type === "minimum_fee") {
      minimumAmount = componentFixed(comp, currency, rule.rule_id);
    } else if (comp.type === "maximum_fee") {
      maximumAmount = componentFixed(comp, currency, rule.rule_id);
    } else if (comp.type === "percentage_surcharge") {
      additivePercentage = additivePercentage.plus(componentRate(comp));
    } else {
      throw new UnsupportedFeeShape("Unsupported Stripe fee component type.", {
        rule_id: rule.rule_id,
        type: comp.type,
      });
    }
  }

  const behavior =
    rule.classification_status && ["free", "included"].includes(rule.classification_status)
      ? "included"
      : (rule.behavior ?? "base");

  if (behavior === "additive") {
    return {
      percentage: additivePercentage.isZero() ? null : additivePercentage.toFixed(),
      fixed_amount: additiveFixed.isZero() ? null : additiveFixed.toFixed(),
      minimum_amount: null,
      maximum_amount: null,
      behavior,
    };
  }

  if (behavior === "included") {
    return {
      percentage: null,
      fixed_amount: null,
      minimum_amount: null,
      maximum_amount: null,
      behavior,
    };
  }

  return {
    percentage: basePercentage.isZero() ? null : basePercentage.toFixed(),
    fixed_amount: baseFixed.isZero() ? null : baseFixed.toFixed(),
    minimum_amount: minimumAmount?.toFixed() ?? null,
    maximum_amount: maximumAmount?.toFixed() ?? null,
    behavior,
  };
}

function componentRate(comp: StripeFeeComponent): Decimal {
  if (comp.basis_points) {
    return toDecimal(comp.basis_points).dividedBy("100");
  }
  if (comp.value) {
    return toDecimal(comp.value);
  }
  throw new UnsupportedFeeShape("Percentage component missing basis_points and value.", {
    component: comp.type,
  });
}

function componentFixed(comp: StripeFeeComponent, currency: string, ruleId: string): Decimal {
  if (!comp.amount) {
    throw new UnsupportedFeeShape("Fixed component missing amount.", {
      component: comp.type,
      rule_id: ruleId,
    });
  }
  const compCurrency = (comp.currency ?? currency).toUpperCase();
  if (compCurrency !== currency.toUpperCase()) {
    throw new QuoteNotAvailable(
      "A selected Stripe fee rule uses a fixed amount in a different currency.",
      {
        rule_id: ruleId,
        component_currency: compCurrency,
        transaction_currency: currency,
      },
    );
  }
  return toDecimal(comp.amount);
}

function executableFromRule(rule: StripeRule, currency: string): ExecutableRule {
  const compiled = compileStripeComponents(rule, currency);
  let componentType = "processing";
  if (compiled.behavior === "additive") componentType = "surcharge";
  if (compiled.behavior === "included") componentType = "included";

  return {
    rule_id: rule.rule_id,
    label: rule.label ?? rule.name ?? rule.rule_id,
    component_type: componentType,
    behavior: compiled.behavior,
    percentage: compiled.percentage,
    fixed_amount: compiled.fixed_amount,
    fixed_currency: compiled.fixed_amount ? currency : null,
    minimum_amount: compiled.minimum_amount,
    maximum_amount: compiled.maximum_amount,
    classification_status: rule.classification_status ?? "unclassified",
    confidence: rule.confidence ?? 0.0,
    exactness: rule.exactness ?? "exact",
    source_url: rule.source_url ?? null,
    payer: rule.payer ?? null,
    unit: rule.unit ?? "per_transaction",
  };
}

function apiFieldName(dimension: string): string {
  const mapping: Record<string, string> = {
    payment_method: "transaction.payment_method",
    payment_method_variant: "transaction.payment_method_variant",
    product_id: "transaction.product_id",
    variant_id: "transaction.variant_id",
    channel: "transaction.channel",
    card_origin: "transaction.card.origin",
    card_region: "transaction.card.region",
    card_tier: "transaction.card.tier",
    card_type: "transaction.card.type",
    card_network: "transaction.card.network",
    card_entry_mode: "transaction.card.entry_mode",
    customer_country: "customer_country",
    presentment_currency: "amount.currency",
    settlement_currency: "settlement_currency",
    settlement_timing: "transaction.settlement.timing",
    currency_conversion_required: "transaction.currency_conversion_required",
    recurring: "transaction.recurring",
    billing_type: "transaction.billing_type",
    pricing_plan: "transaction.pricing_plan",
    pricing_tier: "transaction.pricing_tier",
    product_feature: "transaction.product_feature",
    integration_type: "transaction.integration_type",
    contract_length: "transaction.contract_length",
    dispute_state: "transaction.dispute_state",
    transaction_region: "transaction.transaction_region",
    cross_border: "transaction.cross_border",
    feature_enabled: "transaction.feature_enabled",
    payer: "transaction.payer",
    success: "transaction.context.success",
    bank_account_validation: "transaction.bank.validation",
    bank_transfer_type: "transaction.bank.transfer_type",
    transaction_amount: "amount.value",
  };
  return mapping[dimension] ?? `transaction.context.${dimension}`;
}
