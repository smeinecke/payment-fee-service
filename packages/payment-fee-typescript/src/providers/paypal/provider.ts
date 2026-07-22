import { Decimal } from "decimal.js";
import {
  AmbiguousFeeRules,
  InsufficientTransactionContext,
  QuoteNotAvailable,
  UnknownMarket,
  UnsupportedFeeShape,
} from "../../errors.js";
import type { ExecutableRule } from "../../calculator.js";
import type { PayPalQuoteRequest, QuoteRequest } from "../../models.js";
import { toDecimal } from "../../rounding.js";
import { ScheduleRegistry, type PayPalDerived } from "./schedule-registry.js";
import {
  apiFieldName,
  conditionStatus,
  isEvaluable,
  normalizeConditions,
  specificity,
  type PayPalRule,
  valuesEqual,
} from "./condition-matcher.js";

export type { PayPalRule };

export interface PayPalCountry {
  country_code: string;
  derived?: { status?: string; transaction_fee_rules?: PayPalRule[] };
}

export interface PayPalCore {
  schema_version?: number;
  countries?: PayPalCountry[];
}

function componentScheduleId(rule: PayPalRule, type: string): string | undefined {
  for (const comp of rule.fee_components ?? []) {
    if (comp.type === type) {
      return comp.schedule_id ? comp.schedule_id : undefined;
    }
  }
  return undefined;
}

function buildContext(request: PayPalQuoteRequest): Record<string, unknown> {
  const transaction = request.transaction;
  const context: Record<string, unknown> = {
    account_country: request.account_country.toUpperCase(),
    customer_country: request.customer_country?.toUpperCase() ?? null,
    amount_currency: request.amount.currency.toUpperCase(),
    transaction_amount: request.amount.value,
    product_id: transaction.product_id ?? null,
    variant_id: transaction.variant_id ?? null,
    payment_method: transaction.payment_method ?? null,
    payer_region: transaction.payer_region ?? null,
    surcharge_region: transaction.surcharge_region ?? null,
    merchant_approval_required: transaction.merchant_approval_required ?? null,
    pricing_plan: transaction.pricing_plan ?? null,
    withdrawal_method: transaction.withdrawal_method ?? null,
    authorization_channel: transaction.authorization_channel ?? null,
    point_of_sale: transaction.point_of_sale ?? null,
    card_present: transaction.card_present ?? null,
    transaction_purpose: transaction.transaction_purpose ?? null,
    funding_source: transaction.funding_source ?? null,
    service: transaction.service ?? null,
    recipient_location: transaction.recipient_location ?? null,
    volume_status: transaction.volume_status ?? null,
    fee_currency: (transaction.fee_currency ?? request.amount.currency).toUpperCase(),
  };

  if (transaction.transaction_region !== undefined && transaction.transaction_region !== null) {
    context.transaction_region = String(transaction.transaction_region).toLowerCase();
  } else if (request.customer_country !== undefined && request.customer_country !== null) {
    context.transaction_region =
      request.customer_country.toUpperCase() === request.account_country.toUpperCase()
        ? "domestic"
        : "international";
  } else {
    context.transaction_region = "domestic";
  }

  for (const [key, value] of Object.entries(transaction.context ?? {})) {
    if (value === undefined) {
      continue;
    }
    const existing = context[key];
    if (existing !== undefined && existing !== null) {
      if (!valuesEqual(value, existing)) {
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

  const transactionRegion = String(context.transaction_region ?? "").toLowerCase();
  if (transactionRegion === "international") {
    context.applies_to_markets_target = context.customer_country;
  } else {
    context.applies_to_markets_target = context.account_country;
  }

  return context;
}

function rulePercentage(rule: PayPalRule): string | null {
  if (rule.percentage !== undefined && rule.percentage !== null) {
    return rule.percentage;
  }
  for (const comp of rule.fee_components ?? []) {
    if (comp.type === "percentage" && comp.value) {
      return comp.value;
    }
  }
  return null;
}

function resolveFixedAmount(
  rule: PayPalRule,
  registry: ScheduleRegistry,
  currency: string,
  raiseOnMissing = true,
): { amount: Decimal | null; currency: string | null } {
  let amount: Decimal | null = null;
  let fixedCurrency: string | null = null;

  for (const comp of rule.fee_components ?? []) {
    if (comp.type === "fixed_amount" && comp.amount) {
      amount = (amount ?? new Decimal("0")).plus(comp.amount);
      fixedCurrency = comp.currency ?? currency;
    }
  }

  const scheduleId = rule.fixed_fee_schedule
    ? rule.fixed_fee_schedule
    : componentScheduleId(rule, "fixed_fee_schedule");
  if (scheduleId) {
    const scheduleValue = raiseOnMissing
      ? registry.fixed(scheduleId, currency)
      : registry.fixed(scheduleId, currency, false);
    if (scheduleValue !== undefined) {
      amount = (amount ?? new Decimal("0")).plus(scheduleValue);
      fixedCurrency = currency;
    }
  }

  return { amount, currency: fixedCurrency };
}

function resolveMaximumAmount(
  rule: PayPalRule,
  registry: ScheduleRegistry,
  currency: string,
  raiseOnMissing = true,
): string | null {
  const scheduleId = rule.maximum_fee_schedule
    ? rule.maximum_fee_schedule
    : componentScheduleId(rule, "maximum_fee_schedule");
  if (!scheduleId) {
    return null;
  }
  const value = raiseOnMissing
    ? registry.maximum(scheduleId, currency)
    : registry.maximum(scheduleId, currency, false);
  return value ?? null;
}

function resolveSurchargeRate(
  rule: PayPalRule,
  registry: ScheduleRegistry,
  payerRegion: string | null | undefined,
): Decimal | null {
  const scheduleId = rule.international_surcharge_schedule
    ? rule.international_surcharge_schedule
    : componentScheduleId(rule, "international_surcharge_schedule");
  if (!scheduleId || !payerRegion) {
    return null;
  }
  const rate = registry.surchargeRate(scheduleId, payerRegion);
  return rate !== undefined ? toDecimal(rate) : null;
}

function ruleSignature(
  rule: PayPalRule,
  registry: ScheduleRegistry,
  currency: string,
  payerRegion: string | null | undefined,
): string {
  const percentage = rulePercentage(rule);
  const fixed = resolveFixedAmount(rule, registry, currency, false);
  const maximum = resolveMaximumAmount(rule, registry, currency, false);
  const surchargeRate = resolveSurchargeRate(rule, registry, payerRegion);
  return JSON.stringify([
    percentage ? toDecimal(percentage).toFixed() : null,
    fixed.amount ? toDecimal(fixed.amount).toFixed() : null,
    maximum ? toDecimal(maximum).toFixed() : null,
    surchargeRate ? surchargeRate.toFixed() : null,
  ]);
}

function resolveSurchargeScheduleId(rule: PayPalRule): string | undefined {
  return rule.international_surcharge_schedule
    ? rule.international_surcharge_schedule
    : componentScheduleId(rule, "international_surcharge_schedule");
}

function requireSurchargeRegionContext(
  scheduleId: string | undefined,
  registry: ScheduleRegistry,
  payerRegion: string | null | undefined,
  transactionRegion: string,
  context: { provider: string; market: string },
): void {
  if (!scheduleId || payerRegion != null || transactionRegion === "domestic") {
    return;
  }
  const availableRegions = registry.surchargeRegions(scheduleId);
  throw new InsufficientTransactionContext(
    ["transaction.payer_region", "transaction.surcharge_region"],
    {
      provider: context.provider,
      market: context.market,
      available_surcharge_regions: [...new Set(availableRegions)].sort(),
    },
  );
}

function selectTopEvaluable(
  fullMatches: { rule: PayPalRule; specificity: number }[],
  missingMatches: { rule: PayPalRule; missing: string[]; specificity: number }[],
  registry: ScheduleRegistry,
  currency: string,
  payerRegion: string | null | undefined,
  request: PayPalQuoteRequest,
): PayPalRule {
  const productIdLower = String(request.transaction.product_id).toLowerCase();
  const requestedVariantId = request.transaction.variant_id;
  const variantIdLower =
    requestedVariantId !== undefined && requestedVariantId !== null
      ? String(requestedVariantId).toLowerCase()
      : null;

  if (fullMatches.length === 0) {
    if (missingMatches.length > 0) {
      const allMissing = [...new Set(missingMatches.flatMap((m) => m.missing))].sort();
      throw new InsufficientTransactionContext(allMissing, {
        provider: "paypal",
        market: request.account_country,
        product_id: productIdLower,
        variant_id: variantIdLower,
      });
    }
    throw new QuoteNotAvailable("No fee rule matched the supplied context.", {
      provider: "paypal",
      market: request.account_country,
      product_id: productIdLower,
      variant_id: variantIdLower,
    });
  }

  const maxSpec = Math.max(...fullMatches.map((m) => m.specificity));
  const mostSpecific = fullMatches
    .filter((m) => Math.abs(m.specificity - maxSpec) < 1e-9)
    .map((m) => m.rule);

  if (!mostSpecific.some(isEvaluable)) {
    throw new QuoteNotAvailable("A selected PayPal rule is not calculable.", {
      provider: "paypal",
      market: request.account_country,
      rule_ids: mostSpecific.map((r) => r.id).sort(),
    });
  }

  const selectable = fullMatches.filter((m) => isEvaluable(m.rule));
  const selectMaxSpec = Math.max(...selectable.map((m) => m.specificity));
  const topMatches = selectable
    .filter((m) => Math.abs(m.specificity - selectMaxSpec) < 1e-9)
    .map((m) => m.rule);

  if (topMatches.length > 1) {
    const signatures = new Set<string>();
    for (const rule of topMatches) {
      signatures.add(ruleSignature(rule, registry, currency, payerRegion));
    }
    if (signatures.size > 1) {
      throw new AmbiguousFeeRules(topMatches.map((r) => r.id).sort(), {
        provider: "paypal",
        market: request.account_country,
      });
    }
  }

  return topMatches.sort((a, b) => {
    if (a.id !== b.id) {
      return a.id < b.id ? -1 : 1;
    }
    const aVariant = a.variant_id ?? "";
    const bVariant = b.variant_id ?? "";
    if (aVariant === bVariant) {
      return 0;
    }
    return aVariant < bVariant ? -1 : 1;
  })[0];
}

export class PayPalProvider {
  readonly providerId = "paypal";

  constructor(private readonly core: PayPalCore) {}

  assumptions(_request: QuoteRequest): string[] {
    return [
      "Public standard pricing was used; negotiated merchant pricing is not represented.",
      "The published dataset does not encode provider settlement rounding, so standard currency rounding is used.",
    ];
  }

  data(request: QuoteRequest): Record<string, unknown> {
    return {
      provider: this.providerId,
      schema_version: 1,
      market: request.account_country,
      content_sha256: null,
      source_urls: [],
      source_updated_at: null,
      data_ref: "documents",
    };
  }

  compileRules(request: QuoteRequest): ExecutableRule[] {
    const paypalRequest = request as PayPalQuoteRequest;
    const country = this.findCountry(paypalRequest.account_country);
    const derived = (country.derived ?? {}) as PayPalDerived & {
      transaction_fee_rules?: PayPalRule[];
    };
    const registry = new ScheduleRegistry(derived);
    const rules = derived.transaction_fee_rules ?? [];
    const context = buildContext(paypalRequest);

    const candidateRules = this._candidateRules(rules, paypalRequest);
    const { fullMatches, missingMatches } = this._evaluateCandidates(candidateRules, context);
    const selected = this._selectSingleRule(
      fullMatches,
      missingMatches,
      registry,
      context,
      paypalRequest,
    );
    return this.compileRule(selected, registry, paypalRequest, context);
  }

  private _candidateRules(rules: PayPalRule[], request: PayPalQuoteRequest): PayPalRule[] {
    const availableProductIds = [...new Set(rules.map((rule) => rule.id.toLowerCase()))].sort();
    const requestedProductId = request.transaction.product_id;
    if (
      requestedProductId === undefined ||
      requestedProductId === null ||
      String(requestedProductId) === ""
    ) {
      throw new InsufficientTransactionContext(["transaction.product_id"], {
        provider: "paypal",
        market: request.account_country,
        available_product_ids: availableProductIds,
      });
    }
    const productIdLower = String(requestedProductId).toLowerCase();
    const productRules = rules.filter((rule) => rule.id.toLowerCase() === productIdLower);

    const requestedVariantId = request.transaction.variant_id;
    let variantIdLower: string | null = null;
    let candidateRules: PayPalRule[];
    if (
      requestedVariantId !== undefined &&
      requestedVariantId !== null &&
      String(requestedVariantId) !== ""
    ) {
      variantIdLower = String(requestedVariantId).toLowerCase();
      candidateRules = productRules.filter((rule) => {
        if (rule.variant_id === undefined || rule.variant_id === null) {
          return true;
        }
        return String(rule.variant_id).toLowerCase() === variantIdLower;
      });
    } else {
      candidateRules = productRules;
    }

    if (candidateRules.length === 0) {
      throw new QuoteNotAvailable(
        "The requested PayPal product/variant is not classified for this market.",
        {
          market: request.account_country,
          product_id: productIdLower,
          variant_id: variantIdLower,
        },
      );
    }

    return candidateRules;
  }

  private _evaluateCandidates(
    candidateRules: PayPalRule[],
    context: Record<string, unknown>,
  ): {
    fullMatches: { rule: PayPalRule; specificity: number }[];
    missingMatches: { rule: PayPalRule; missing: string[]; specificity: number }[];
  } {
    const fullMatches: { rule: PayPalRule; specificity: number }[] = [];
    const missingMatches: { rule: PayPalRule; missing: string[]; specificity: number }[] = [];

    for (const rule of candidateRules) {
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
      if (conflict) {
        continue;
      }
      const spec = specificity(rule);
      if (missing.length > 0) {
        missingMatches.push({ rule, missing: [...new Set(missing)].sort(), specificity: spec });
      } else {
        fullMatches.push({ rule, specificity: spec });
      }
    }

    return { fullMatches, missingMatches };
  }

  private _selectSingleRule(
    fullMatches: { rule: PayPalRule; specificity: number }[],
    missingMatches: { rule: PayPalRule; missing: string[]; specificity: number }[],
    registry: ScheduleRegistry,
    context: Record<string, unknown>,
    request: PayPalQuoteRequest,
  ): PayPalRule {
    const currency = request.amount.currency;
    const payerRegion = ((context.payer_region as string | null | undefined) ??
      context.surcharge_region) as string | null | undefined;
    const transactionRegion = String(context.transaction_region ?? "").toLowerCase();

    const selected = selectTopEvaluable(
      fullMatches,
      missingMatches,
      registry,
      currency,
      payerRegion,
      request,
    );
    requireSurchargeRegionContext(
      resolveSurchargeScheduleId(selected),
      registry,
      payerRegion,
      transactionRegion,
      { provider: "paypal", market: request.account_country },
    );
    return selected;
  }

  private compileRule(
    rule: PayPalRule,
    registry: ScheduleRegistry,
    request: PayPalQuoteRequest,
    context: Record<string, unknown>,
  ): ExecutableRule[] {
    const currency = request.amount.currency;
    const payerRegion = ((context.payer_region as string | null | undefined) ??
      context.surcharge_region) as string | null | undefined;
    const transactionRegion = String(context.transaction_region ?? "").toLowerCase();

    this._assertSupportedFeeComponents(rule);

    const { amount: fixedAmount, currency: fixedCurrency } = resolveFixedAmount(
      rule,
      registry,
      currency,
    );
    const percentage = rulePercentage(rule);
    const maximumAmount = resolveMaximumAmount(rule, registry, currency);

    const executable = this._buildBaseExecutable(
      rule,
      request,
      percentage,
      fixedAmount,
      fixedCurrency,
      maximumAmount,
    );

    const scheduleId = resolveSurchargeScheduleId(rule);
    if (!scheduleId) {
      return [executable];
    }
    requireSurchargeRegionContext(scheduleId, registry, payerRegion, transactionRegion, {
      provider: "paypal",
      market: request.account_country,
    });

    const surcharge = registry.surcharge(scheduleId, payerRegion, currency);
    if (!surcharge || (surcharge.percentage === null && surcharge.fixed_amount === null)) {
      return [executable];
    }

    return [executable, this._buildSurchargeExecutable(rule, request, surcharge, payerRegion)];
  }

  private _assertSupportedFeeComponents(rule: PayPalRule): void {
    const supportedTypes = new Set([
      "percentage",
      "fixed_amount",
      "fixed_fee_schedule",
      "international_surcharge_schedule",
      "maximum_fee_schedule",
    ]);
    for (const comp of rule.fee_components ?? []) {
      if (!supportedTypes.has(comp.type)) {
        throw new UnsupportedFeeShape(`Unsupported PayPal fee component type: ${comp.type}`, {
          rule_id: rule.id,
        });
      }
    }
  }

  private _buildBaseExecutable(
    rule: PayPalRule,
    request: PayPalQuoteRequest,
    percentage: string | null,
    fixedAmount: Decimal | null,
    fixedCurrency: string | null,
    maximumAmount: string | null,
  ): ExecutableRule {
    return {
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
  }

  private _buildSurchargeExecutable(
    rule: PayPalRule,
    request: PayPalQuoteRequest,
    surcharge: {
      percentage?: string | null;
      fixed_amount?: string | null;
      fixed_currency?: string | null;
    },
    payerRegion: string | null | undefined,
  ): ExecutableRule {
    const currency = request.amount.currency;
    return {
      rule_id: `paypal:${request.account_country}:${rule.id}:${rule.variant_id ?? "default"}:surcharge:${payerRegion ?? "unknown"}`,
      label: `International surcharge (${payerRegion ?? "unknown"})`,
      component_type: "surcharge",
      behavior: "additive",
      percentage: surcharge.percentage ?? null,
      fixed_amount: surcharge.fixed_amount ?? null,
      fixed_currency: surcharge.fixed_amount ? (surcharge.fixed_currency ?? currency) : null,
      minimum_amount: null,
      maximum_amount: null,
      classification_status: rule.calculation_status ?? "calculable",
      confidence: 1.0,
      exactness: "exact",
      source_url: null,
    };
  }

  auditContract(): Record<string, number> {
    let total = 0;
    let parsed = 0;
    let skipped = 0;
    let contextRequired = 0;

    for (const country of this.core.countries ?? []) {
      for (const rule of (country.derived as { transaction_fee_rules?: PayPalRule[] })
        .transaction_fee_rules ?? []) {
        total += 1;
        const status = rule.calculation_status ?? "calculable";
        if (status !== "calculable") {
          skipped += 1;
          continue;
        }
        const supported = (rule.fee_components ?? []).every((comp) =>
          [
            "fixed_amount",
            "fixed_fee_schedule",
            "percentage",
            "international_surcharge_schedule",
            "maximum_fee_schedule",
          ].includes(comp.type),
        );
        if (!supported) {
          skipped += 1;
          continue;
        }
        if (rule.conditions && Object.keys(rule.conditions).length > 0) {
          contextRequired += 1;
        }
        parsed += 1;
      }
    }

    return {
      paypal_calculable_rules_total: total,
      paypal_calculable_rules_parsed: parsed,
      paypal_calculable_rules_skipped: skipped,
      paypal_context_required: contextRequired,
    };
  }

  private findCountry(code: string): PayPalCountry {
    for (const country of this.core.countries ?? []) {
      if (country.country_code.toUpperCase() === code.toUpperCase()) {
        return country;
      }
    }
    throw new UnknownMarket("paypal", code);
  }
}
