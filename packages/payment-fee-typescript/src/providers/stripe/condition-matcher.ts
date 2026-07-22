import { Decimal } from "decimal.js";
import { QuoteNotAvailable, UnsupportedFeeShape } from "../../errors.js";
import type { ExecutableRule } from "../../calculator.js";
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
  payment_method_variant?: string | null;
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
  // Additional dimensions that may appear as top-level rule fields
  additional_fees?: unknown[];
  fixed_amount_minor?: string | null;
  channel?: string | null;
  card_network?: string | null;
  card_origin?: string | null;
  card_region?: string | null;
  card_tier?: string | null;
  card_type?: string | null;
  card_entry_mode?: string | null;
  contract_length?: string | null;
  cross_border?: boolean | null;
  currency_conversion_required?: boolean | null;
  customer_country?: string | null;
  dispute_state?: string | null;
  feature_enabled?: string | null;
  fee_type?: string | null;
  integration_type?: string | null;
  presentment_currency?: string | null;
  pricing_plan?: string | null;
  pricing_tier?: string | null;
  product_feature?: string | null;
  recurring?: boolean | null;
  billing_type?: string | null;
  settlement_currency?: string | null;
  settlement_timing?: string | null;
  success?: boolean | null;
  transaction_amount_max?: string | null;
  transaction_amount_min?: string | null;
  transaction_region?: string | null;
  transaction_type?: string | null;
  bank_account_validation?: string | null;
  bank_transfer_type?: string | null;
}

const EVALUABLE_CLASSIFICATION_STATUSES = new Set(["calculable_rule", "free", "included"]);

export function normalizeConditions(
  rule: StripeRule,
): { dimension: string; operator: string; value: unknown }[] {
  const conditions: { dimension: string; operator: string; value: unknown }[] = [];
  const topLevel: [string, unknown][] = [
    ["account_country", rule.account_country],
    ["payment_method", rule.payment_method],
    ["payment_method_variant", rule.payment_method_variant ?? null],
    ["product_id", rule.product_id],
    ["variant_id", rule.variant_id],
    ["channel", rule.channel],
    ["card_origin", rule.card_origin],
    ["card_region", rule.card_region],
    ["card_tier", rule.card_tier],
    ["card_type", rule.card_type],
    ["card_network", rule.card_network],
    ["card_entry_mode", rule.card_entry_mode],
    ["customer_country", rule.customer_country],
    ["presentment_currency", rule.presentment_currency],
    ["settlement_currency", rule.settlement_currency],
    ["settlement_timing", rule.settlement_timing],
    ["currency_conversion_required", rule.currency_conversion_required],
    ["recurring", rule.recurring],
    ["billing_type", rule.billing_type],
    ["pricing_plan", rule.pricing_plan],
    ["pricing_tier", rule.pricing_tier],
    ["product_feature", rule.product_feature],
    ["integration_type", rule.integration_type],
    ["contract_length", rule.contract_length],
    ["dispute_state", rule.dispute_state],
    ["transaction_region", rule.transaction_region],
    ["transaction_type", rule.transaction_type],
    ["cross_border", rule.cross_border],
    ["feature_enabled", rule.feature_enabled],
    ["payer", rule.payer],
    ["success", rule.success],
    ["bank_account_validation", rule.bank_account_validation],
    ["bank_transfer_type", rule.bank_transfer_type],
    ["fee_type", rule.fee_type],
  ];
  for (const [dimension, value] of topLevel) {
    if (value !== undefined && value !== null) {
      conditions.push({ dimension, operator: "eq", value });
    }
  }

  if (rule.transaction_amount_min !== undefined && rule.transaction_amount_min !== null) {
    conditions.push({
      dimension: "transaction_amount",
      operator: "gte",
      value: rule.transaction_amount_min,
    });
  }
  if (rule.transaction_amount_max !== undefined && rule.transaction_amount_max !== null) {
    conditions.push({
      dimension: "transaction_amount",
      operator: "lte",
      value: rule.transaction_amount_max,
    });
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

export function conditionStatus(
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

export function isEvaluable(rule: StripeRule): boolean {
  return EVALUABLE_CLASSIFICATION_STATUSES.has(rule.classification_status ?? "unclassified");
}

export function sortBySpecificityDesc(
  matches: { rule: StripeRule; specificity: number }[],
): { rule: StripeRule; specificity: number }[] {
  return [...matches].sort((a, b) => b.specificity - a.specificity);
}

export function selectAdditiveRules(
  rules: StripeRule[],
  context: Record<string, unknown>,
): StripeRule[] {
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

export function compileStripeComponents(
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

export function executableFromRule(rule: StripeRule, currency: string): ExecutableRule {
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

export function apiFieldName(dimension: string): string {
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
    account_country: "account_country",
    customer_country: "customer_country",
    amount_currency: "amount.currency",
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
    transaction_type: "transaction.context.transaction_type",
    cross_border: "transaction.cross_border",
    feature_enabled: "transaction.feature_enabled",
    payer: "transaction.payer",
    unit: "transaction.unit",
    success: "transaction.context.success",
    bank_account_validation: "transaction.bank.validation",
    bank_transfer_type: "transaction.bank.transfer_type",
    fee_type: "transaction.context.fee_type",
    transaction_amount: "amount.value",
  };
  return mapping[dimension] ?? `transaction.context.${dimension}`;
}
