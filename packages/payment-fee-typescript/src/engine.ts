import { calculate } from "./calculator.js";
import type { ExecutableRule } from "./calculator.js";
import { UnknownProvider } from "./errors.js";
import type { QuoteRequest } from "./models.js";
import { PayPalProvider, type PayPalCore } from "./providers/paypal/provider.js";
import { StripeProvider, type StripeCore } from "./providers/stripe/provider.js";

function deriveStatus(rules: ExecutableRule[]): string {
  const nonAdditive = rules.filter((r) => (r.behavior ?? "base") !== "additive");
  if (
    nonAdditive.length > 0 &&
    nonAdditive.every((r) => ["free", "included", "waived"].includes(r.behavior ?? "base"))
  ) {
    return "included";
  }
  for (const rule of rules) {
    if (["range", "from", "up_to"].includes(rule.exactness ?? "")) {
      return "range";
    }
  }
  for (const rule of rules) {
    if (
      (rule.exactness ?? "") !== "" &&
      !["exact", "exact_for_public_rate"].includes(rule.exactness ?? "")
    ) {
      return "estimated";
    }
    const status = rule.classification_status ?? "";
    if (
      status !== "" &&
      !["calculable", "calculable_rule", "exact", "exact_for_public_rate"].includes(status)
    ) {
      return "estimated";
    }
  }
  return "exact_for_public_rate";
}

interface Provider {
  compileRules(request: QuoteRequest): ExecutableRule[];
  auditContract(): Record<string, number>;
}

export class PaymentFeeEngine {
  private readonly _providers = new Map<string, Provider>();

  static async fromPaths(args: {
    paypal?: string;
    stripe?: string;
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    const { readFile } = await import("node:fs/promises");
    const engine = new PaymentFeeEngine();
    if (args.paypal) {
      const core = JSON.parse(
        await readFile(`${args.paypal}/json/core-fees.json`, "utf-8"),
      ) as PayPalCore;
      engine.register("paypal", new PayPalProvider(core));
    }
    if (args.stripe) {
      const core = JSON.parse(
        await readFile(`${args.stripe}/json/core-fees.json`, "utf-8"),
      ) as StripeCore;
      engine.register("stripe", new StripeProvider(core));
    }
    return engine;
  }

  static async fromDocuments(args: {
    paypal?: PayPalCore | { core?: PayPalCore };
    stripe?: StripeCore | { core?: StripeCore };
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    await Promise.resolve();
    const engine = new PaymentFeeEngine();
    if (args.paypal) {
      const paypal = args.paypal;
      const core = ("core" in paypal ? paypal.core : undefined) ?? (paypal as PayPalCore);
      engine.register("paypal", new PayPalProvider(core));
    }
    if (args.stripe) {
      const stripe = args.stripe;
      const core = ("core" in stripe ? stripe.core : undefined) ?? (stripe as StripeCore);
      engine.register("stripe", new StripeProvider(core));
    }
    return engine;
  }

  register(provider: string, instance: Provider): void {
    this._providers.set(provider, instance);
  }

  quote(request: QuoteRequest): Record<string, unknown> {
    this.requireProvider(request.provider);
    const provider = this._providers.get(request.provider)!;
    const rules = provider.compileRules(request);
    const result = calculate(request.amount, request.amount.currency, rules);
    const status = deriveStatus(rules);

    if (request.provider === "paypal") {
      return {
        provider: "paypal",
        status,
        amount: result.amount,
        processing_fee: result.processing_fee,
        net_amount: result.net_amount,
        components: result.components,
        matched_rules: result.matched_rules,
        selected_product_id: request.transaction.product_id,
        selected_variant_id: request.transaction.variant_id,
        assumptions: [
          "Public standard pricing was used; negotiated merchant pricing is not represented.",
          "The published dataset does not encode provider settlement rounding, so standard currency rounding is used.",
        ],
        warnings: [],
        data: {
          provider: "paypal",
          schema_version: 1,
          market: request.account_country,
          content_sha256: null,
          source_urls: [],
          source_updated_at: null,
          data_ref: "documents",
        },
      };
    }

    const stripeAssumptions = [
      "Public standard pricing was used; negotiated or IC++ pricing is not represented.",
      "The published dataset does not encode provider settlement rounding, so standard currency rounding is used.",
    ];
    const successValue = request.transaction.context?.success;
    if (successValue === true || successValue === undefined) {
      stripeAssumptions.push("Assumed a successful transaction for providers that require success.");
    }

    return {
      provider: "stripe",
      status,
      amount: result.amount,
      processing_fee: result.processing_fee,
      net_amount: result.net_amount,
      components: result.components,
      matched_rules: result.matched_rules,
      selected_product_id: request.transaction.product_id,
      selected_variant_id: request.transaction.variant_id,
      assumptions: stripeAssumptions,
      warnings: [],
      data: {
        provider: "stripe",
        schema_version: 1,
        market: request.account_country,
        content_sha256: null,
        source_urls: [],
        source_updated_at: null,
        data_ref: "documents",
      },
    };
  }

  providers(): string[] {
    return [...this._providers.keys()];
  }

  markets(_provider: string): Record<string, unknown>[] {
    this.requireProvider(_provider);
    return [];
  }

  capabilities(_provider: string, _market: string): Record<string, unknown> {
    this.requireProvider(_provider);
    return {};
  }

  quoteSchema(_provider: string, _market: string): Record<string, unknown> {
    this.requireProvider(_provider);
    return {};
  }

  dataStatus(): Record<string, unknown>[] {
    return [];
  }

  auditContract(): Record<string, number> {
    const result: Record<string, number> = {};
    for (const provider of this._providers.values()) {
      const audit = provider.auditContract();
      for (const [key, value] of Object.entries(audit)) {
        result[key] = (result[key] ?? 0) + value;
      }
    }
    const required = [
      "paypal_calculable_rules_total",
      "paypal_calculable_rules_parsed",
      "paypal_calculable_rules_skipped",
      "paypal_context_required",
      "stripe_calculable_rules_total",
      "stripe_calculable_rules_parsed",
      "stripe_calculable_rules_skipped",
      "stripe_context_required",
      "unknown_fields",
      "unknown_condition_dimensions",
      "unknown_condition_operators",
      "unsupported_fee_components",
      "unresolved_schedule_references",
    ];
    for (const key of required) {
      if (!(key in result)) {
        result[key] = 0;
      }
    }
    return result;
  }

  private requireProvider(provider: string): void {
    if (!this._providers.has(provider)) {
      throw new UnknownProvider(provider);
    }
  }
}
