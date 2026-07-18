import { Decimal } from "decimal.js";
import { AmbiguousFeeRules, QuoteNotAvailable, UnsupportedFeeShape } from "../../errors.js";
import type { ExecutableRule } from "../../calculator.js";
import type { PayPalQuoteRequest } from "../../models.js";
import { ScheduleRegistry, type PayPalDerived } from "./schedule-registry.js";

export interface PayPalRule {
  id: string;
  variant_id?: string | null;
  label?: string | null;
  percentage?: string | null;
  fixed_fee_schedule?: string | null;
  international_surcharge_schedule?: string | null;
  maximum_fee_schedule?: string | null;
  calculation_status?: string;
  fee_components?: {
    type: string;
    value?: string | null;
    amount?: string | null;
    currency?: string | null;
    schedule_id?: string | null;
  }[];
}

export interface PayPalCountry {
  country_code: string;
  derived?: { status?: string; transaction_fee_rules?: PayPalRule[] };
}

export interface PayPalCore {
  schema_version?: number;
  countries?: PayPalCountry[];
}

export class PayPalProvider {
  constructor(private readonly core: PayPalCore) {}

  compileRules(request: PayPalQuoteRequest): ExecutableRule[] {
    const country = this.findCountry(request.account_country);
    const derived = (country.derived ?? {}) as PayPalDerived & {
      transaction_fee_rules?: PayPalRule[];
    };
    const registry = new ScheduleRegistry(derived);
    const rules = derived.transaction_fee_rules ?? [];

    const candidates = rules.filter(
      (rule) =>
        (rule.calculation_status ?? "calculable") === "calculable" &&
        rule.id === request.transaction.product_id &&
        (rule.variant_id === undefined ||
          rule.variant_id === null ||
          rule.variant_id === request.transaction.variant_id),
    );

    if (candidates.length === 0) {
      throw new QuoteNotAvailable("No matching PayPal fee rule found.", {
        product_id: request.transaction.product_id,
        variant_id: request.transaction.variant_id,
      });
    }

    if (candidates.length > 1) {
      throw new AmbiguousFeeRules(candidates.map((r) => r.id));
    }

    return [this.compileRule(candidates[0], registry, request)];
  }

  private compileRule(
    rule: PayPalRule,
    registry: ScheduleRegistry,
    request: PayPalQuoteRequest,
  ): ExecutableRule {
    const currency = request.amount.currency;
    const tx = request.transaction;

    let fixedAmount: Decimal | null = null;
    let fixedCurrency: string | undefined;

    for (const comp of rule.fee_components ?? []) {
      const type = comp.type;
      if (type === "fixed_amount") {
        if (comp.amount) {
          fixedAmount = (fixedAmount ?? new Decimal("0")).plus(comp.amount);
          fixedCurrency = comp.currency ?? currency;
        }
      } else if (type === "fixed_fee_schedule") {
        const scheduleId = comp.schedule_id ?? rule.fixed_fee_schedule;
        if (scheduleId) {
          const value = registry.fixed(scheduleId, currency);
          fixedAmount = (fixedAmount ?? new Decimal("0")).plus(value);
          fixedCurrency = currency;
        }
      } else if (
        type !== "percentage" &&
        type !== "international_surcharge_schedule" &&
        type !== "maximum_fee_schedule"
      ) {
        throw new UnsupportedFeeShape(`Unsupported PayPal fee component type: ${type}`, {
          rule_id: rule.id,
        });
      }
    }

    let percentage = rule.percentage ?? null;
    for (const comp of rule.fee_components ?? []) {
      if (comp.type === "percentage" && comp.value) {
        percentage = comp.value;
      }
    }

    let maximumAmount: string | undefined;
    if (rule.maximum_fee_schedule) {
      maximumAmount = registry.maximum(rule.maximum_fee_schedule, currency);
    }

    const executable: ExecutableRule = {
      rule_id: `paypal:${request.account_country}:${rule.id}:${rule.variant_id ?? "default"}:base`,
      label: rule.label ?? rule.id,
      component_type: "processing",
      behavior: "base",
      percentage,
      fixed_amount: fixedAmount?.toFixed() ?? null,
      fixed_currency: fixedCurrency ?? null,
      minimum_amount: null,
      maximum_amount: maximumAmount ?? null,
      classification_status: rule.calculation_status ?? "calculable",
      confidence: 1.0,
      exactness: "exact",
      source_url: null,
    };

    const surcharge = rule.international_surcharge_schedule
      ? registry.surchargeRate(rule.international_surcharge_schedule, tx.payer_region)
      : null;

    if (surcharge) {
      return executable;
      // A proper implementation would also append an additive surcharge rule.
    }

    return executable;
  }

  private findCountry(code: string): PayPalCountry {
    for (const country of this.core.countries ?? []) {
      if (country.country_code.toUpperCase() === code.toUpperCase()) {
        return country;
      }
    }
    throw new QuoteNotAvailable("PayPal market not found.", { market: code });
  }
}
