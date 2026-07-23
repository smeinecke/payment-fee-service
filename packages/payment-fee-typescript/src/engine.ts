import { calculate } from "./calculator.js";
import type { ExecutableRule } from "./calculator.js";
import {
  DEFAULT_PAYPAL_URL,
  DEFAULT_STRIPE_URL,
  dataLocationFromString,
  JsonDataSource,
} from "./data-source.js";
import { UnknownProvider } from "./errors.js";
import type { QuoteRequest } from "./models.js";
import { PayPalProvider, type PayPalCore } from "./providers/paypal/provider.js";
import { StripeProvider, type StripeCore } from "./providers/stripe/provider.js";

export interface Provider {
  readonly providerId: string;
  compileRules(request: QuoteRequest): ExecutableRule[];
  auditContract(): Record<string, number>;
  assumptions(request: QuoteRequest): string[];
  data(request: QuoteRequest): Record<string, unknown>;
}

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
      engine.register(new PayPalProvider(core));
    }
    if (args.stripe) {
      const core = JSON.parse(
        await readFile(`${args.stripe}/json/core-fees.json`, "utf-8"),
      ) as StripeCore;
      engine.register(new StripeProvider(core));
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
      engine.register(new PayPalProvider(core));
    }
    if (args.stripe) {
      const stripe = args.stripe;
      const core = ("core" in stripe ? stripe.core : undefined) ?? (stripe as StripeCore);
      engine.register(new StripeProvider(core));
    }
    return engine;
  }

  static async fromRemote(args: {
    paypal?: string | null;
    stripe?: string | null;
    paypalDataRef?: string;
    stripeDataRef?: string;
    cacheDir?: string;
    ttlSeconds?: number;
    autoRefresh?: boolean;
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    const engine = new PaymentFeeEngine();

    async function load(providerId: "paypal" | "stripe", url: string, dataRef?: string) {
      const location = dataLocationFromString(providerId, url, dataRef);
      const source = new JsonDataSource(location, {
        cacheDir: args.cacheDir,
        ttlSeconds: args.ttlSeconds,
        autoRefresh: args.autoRefresh,
      });
      const core = await source.readJson<PayPalCore | StripeCore>("json/core-fees.json");
      if (providerId === "paypal") {
        engine.register(new PayPalProvider(core as PayPalCore));
      } else {
        engine.register(new StripeProvider(core as StripeCore));
      }
    }

    const paypalUrl =
      args.paypal === undefined && args.stripe === undefined
        ? DEFAULT_PAYPAL_URL
        : (args.paypal ?? null);
    const stripeUrl =
      args.paypal === undefined && args.stripe === undefined
        ? DEFAULT_STRIPE_URL
        : (args.stripe ?? null);
    if (paypalUrl) await load("paypal", paypalUrl, args.paypalDataRef);
    if (stripeUrl) await load("stripe", stripeUrl, args.stripeDataRef);
    return engine;
  }

  register(instance: Provider): void {
    this._providers.set(instance.providerId, instance);
  }

  quote(request: QuoteRequest): Record<string, unknown> {
    this.requireProvider(request.provider);
    const provider = this._providers.get(request.provider)!;
    const rules = provider.compileRules(request);
    const result = calculate(request.amount, request.amount.currency, rules);
    const status = deriveStatus(rules);

    return {
      provider: provider.providerId,
      status,
      amount: result.amount,
      processing_fee: result.processing_fee,
      net_amount: result.net_amount,
      components: result.components,
      matched_rules: result.matched_rules,
      selected_product_id: request.transaction.product_id,
      selected_variant_id: request.transaction.variant_id,
      assumptions: provider.assumptions(request),
      warnings: [],
      data: provider.data(request),
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
    for (const key of [
      "unknown_fields",
      "unknown_condition_dimensions",
      "unknown_condition_operators",
      "unsupported_fee_components",
      "unresolved_schedule_references",
    ]) {
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
