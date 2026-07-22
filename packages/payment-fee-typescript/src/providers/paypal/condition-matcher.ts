import { Decimal } from "decimal.js";
import { UnsupportedFeeShape } from "../../errors.js";

export interface PayPalFeeComponent {
  type: string;
  value?: string | null;
  amount?: string | null;
  currency?: string | null;
  schedule_id?: string | null;
}

export interface PayPalRule {
  id: string;
  variant_id?: string | null;
  label?: string | null;
  percentage?: string | null;
  fixed_fee_schedule?: string | null;
  international_surcharge_schedule?: string | null;
  maximum_fee_schedule?: string | null;
  calculation_status?: string;
  conditions?: Record<string, unknown>;
  fee_components?: PayPalFeeComponent[];
}

export interface NormalizedCondition {
  dimension: string;
  operator: string;
  value: unknown;
}

export function normalizeConditions(rule: PayPalRule): NormalizedCondition[] {
  const conditions: NormalizedCondition[] = [];
  for (const [dimension, expected] of Object.entries(rule.conditions ?? {})) {
    if (dimension === "amount") {
      if (isPlainObject(expected)) {
        const currency = expected.currency;
        if (currency !== undefined && currency !== null) {
          conditions.push({ dimension: "amount_currency", operator: "eq", value: currency });
        }
        const operator = String(expected.operator ?? "eq").toLowerCase();
        const value = expected.value;
        conditions.push({ dimension: "transaction_amount", operator, value });
      }
      continue;
    }

    if (dimension === "applies_to_markets") {
      const values = asList(expected);
      if (values.some((v) => String(v).toLowerCase() === "all_other_markets")) {
        continue;
      }
      conditions.push({ dimension: "applies_to_markets_target", operator: "in", value: values });
      continue;
    }

    if (dimension === "payment_methods") {
      conditions.push({ dimension: "payment_method", operator: "in", value: asList(expected) });
      continue;
    }

    if (Array.isArray(expected)) {
      conditions.push({ dimension, operator: "in", value: expected });
    } else {
      conditions.push({ dimension, operator: "eq", value: expected });
    }
  }

  return conditions;
}

export function conditionStatus(
  condition: NormalizedCondition,
  context: Record<string, unknown>,
): "match" | "conflict" | "missing" {
  const actual = context[condition.dimension];
  const expected = condition.value;
  const operator = condition.operator;

  if ((actual === undefined || actual === null) && expected !== undefined && expected !== null) {
    return "missing";
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

export function valuesEqual(left: unknown, right: unknown): boolean {
  if (typeof left === "boolean" && typeof right === "boolean") {
    return left === right;
  }
  if (typeof left === "boolean" || typeof right === "boolean") {
    return false;
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

export function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [value];
}

export function apiFieldName(dimension: string): string {
  const mapping: Record<string, string> = {
    product_id: "transaction.product_id",
    variant_id: "transaction.variant_id",
    payment_method: "transaction.payment_method",
    transaction_region: "transaction.transaction_region",
    payer_region: "transaction.payer_region",
    surcharge_region: "transaction.surcharge_region",
    applies_to_markets_target: "customer_country",
    customer_country: "customer_country",
    merchant_approval_required: "transaction.merchant_approval_required",
    pricing_plan: "transaction.pricing_plan",
    withdrawal_method: "transaction.withdrawal_method",
    authorization_channel: "transaction.authorization_channel",
    point_of_sale: "transaction.point_of_sale",
    card_present: "transaction.card_present",
    transaction_purpose: "transaction.transaction_purpose",
    funding_source: "transaction.funding_source",
    service: "transaction.service",
    recipient_location: "transaction.recipient_location",
    volume_status: "transaction.volume_status",
    fee_currency: "transaction.fee_currency",
    amount_currency: "amount.currency",
    transaction_amount: "amount.value",
  };
  return mapping[dimension] ?? `transaction.context.${dimension}`;
}

export function specificity(rule: PayPalRule): number {
  let score = 0.0;
  if (rule.variant_id) {
    score += 0.5;
  }
  for (const [dimension, expected] of Object.entries(rule.conditions ?? {})) {
    if (dimension === "amount") {
      score += 1.0;
    } else if (dimension === "applies_to_markets") {
      const values = asList(expected);
      if (values.some((v) => String(v).toLowerCase() === "all_other_markets")) {
        score += 1.0;
      } else {
        score += 1.0 + 1.0 / Math.max(values.length, 1);
      }
    } else if (dimension === "payment_methods") {
      const values = asList(expected);
      score += 1.0 + 1.0 / Math.max(values.length, 1);
    } else if (dimension === "pricing_plan") {
      score += 2.0;
    } else {
      score += 1.0;
    }
  }
  return score;
}

export function isEvaluable(rule: PayPalRule): boolean {
  return (rule.calculation_status ?? "calculable") === "calculable";
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
