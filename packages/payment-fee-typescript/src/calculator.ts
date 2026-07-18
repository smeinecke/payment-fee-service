import { Decimal } from "decimal.js";
import { CurrencyMismatch, QuoteNotAvailable } from "./errors.js";
import type { MoneyLike } from "./money.js";
import { roundMoney, toDecimal, toMoneyString } from "./rounding.js";

export interface ExecutableRule {
  rule_id: string;
  label: string;
  component_type?: string;
  behavior?: string;
  percentage?: string | null;
  basis_points?: string | null;
  fixed_amount?: string | null;
  fixed_currency?: string | null;
  minimum_amount?: string | null;
  maximum_amount?: string | null;
  classification_status?: string;
  confidence?: number;
  exactness?: string;
  source_url?: string | null;
  payer?: string | null;
  unit?: string | null;
}

export interface FeeComponent {
  type: string;
  label: string;
  amount: string;
  currency: string;
  rate_percentage?: string;
  fixed_amount?: string | null;
  minimum_applied: boolean;
  maximum_applied: boolean;
  payer?: string | null;
  unit?: string | null;
  source_rule_id: string;
}

export interface QuoteResult {
  amount: { value: string; currency: string };
  processing_fee: { value: string; currency: string };
  net_amount: { value: string; currency: string };
  components: FeeComponent[];
  matched_rules: MatchedRule[];
}

export interface MatchedRule {
  rule_id: string;
  classification_status: string;
  confidence: number | null;
  exactness: string | null;
  source_url: string | null;
}

export function calculate(
  amount: MoneyLike,
  currency: string,
  rules: ExecutableRule[],
): QuoteResult {
  const components: FeeComponent[] = [];
  const matchedRules: MatchedRule[] = [];
  let rawTotal = new Decimal("0");

  for (const rule of rules) {
    if (["free", "included", "waived"].includes(rule.behavior ?? "")) {
      components.push({
        type: "included",
        label: rule.label,
        amount: "0",
        currency,
        minimum_applied: false,
        maximum_applied: false,
        source_rule_id: rule.rule_id,
      });
      matchedRules.push(matchedRule(rule));
      continue;
    }

    const component = calculateRule(amount, currency, rule);
    components.push(component);
    rawTotal = rawTotal.plus(component.amount);
    matchedRules.push(matchedRule(rule));
  }

  if (components.length === 0) {
    throw new QuoteNotAvailable("No calculable fee components were produced.");
  }

  const processingFee = roundMoney(rawTotal, currency);
  const net = roundMoney(new Decimal(amount.value).minus(processingFee), currency);

  return {
    amount: {
      value: toMoneyString(new Decimal(amount.value), amount.currency),
      currency: amount.currency,
    },
    processing_fee: { value: toMoneyString(processingFee, currency), currency },
    net_amount: { value: toMoneyString(net, currency), currency },
    components,
    matched_rules: matchedRules,
  };
}

function calculateRule(amount: MoneyLike, currency: string, rule: ExecutableRule): FeeComponent {
  if (
    rule.fixed_amount !== undefined &&
    rule.fixed_amount !== null &&
    rule.fixed_currency &&
    rule.fixed_currency !== currency
  ) {
    throw new CurrencyMismatch("A selected fee rule uses a fixed amount in a different currency.", {
      rule_id: rule.rule_id,
      fixed_currency: rule.fixed_currency,
      transaction_currency: currency,
    });
  }

  let raw = new Decimal("0");
  let ratePercentage: string | undefined;

  if (rule.basis_points !== undefined && rule.basis_points !== null) {
    const bp = toDecimal(rule.basis_points);
    ratePercentage = bp.dividedBy("100").toFixed();
    raw = new Decimal(amount.value).times(bp).dividedBy("10000");
  } else if (rule.percentage !== undefined && rule.percentage !== null) {
    const pct = toDecimal(rule.percentage);
    ratePercentage = pct.toFixed();
    raw = new Decimal(amount.value).times(pct).dividedBy("100");
  }

  if (rule.fixed_amount !== undefined && rule.fixed_amount !== null) {
    raw = raw.plus(toDecimal(rule.fixed_amount));
  }

  let minimumApplied = false;
  let maximumApplied = false;

  if (
    rule.minimum_amount !== undefined &&
    rule.minimum_amount !== null &&
    raw.lessThan(toDecimal(rule.minimum_amount))
  ) {
    raw = toDecimal(rule.minimum_amount);
    minimumApplied = true;
  }

  if (
    rule.maximum_amount !== undefined &&
    rule.maximum_amount !== null &&
    raw.greaterThan(toDecimal(rule.maximum_amount))
  ) {
    raw = toDecimal(rule.maximum_amount);
    maximumApplied = true;
  }

  const rounded = roundMoney(raw, currency);

  const component: FeeComponent = {
    type: rule.component_type ?? "processing",
    label: rule.label,
    amount: toMoneyString(rounded, currency),
    currency,
    minimum_applied: minimumApplied,
    maximum_applied: maximumApplied,
    source_rule_id: rule.rule_id,
  };

  if (ratePercentage !== undefined) {
    component.rate_percentage = ratePercentage;
  }
  if (rule.fixed_amount !== undefined && rule.fixed_amount !== null) {
    component.fixed_amount = toMoneyString(
      toDecimal(rule.fixed_amount),
      rule.fixed_currency ?? currency,
    );
  }

  if (rule.payer !== undefined && rule.payer !== null) {
    component.payer = rule.payer;
  }
  if (rule.unit !== undefined && rule.unit !== null) {
    component.unit = rule.unit;
  }

  return component;
}

function matchedRule(rule: ExecutableRule): MatchedRule {
  return {
    rule_id: rule.rule_id,
    classification_status: rule.classification_status ?? "calculable",
    confidence: rule.confidence ?? 1.0,
    exactness: rule.exactness ?? "exact",
    source_url: rule.source_url ?? null,
  };
}
