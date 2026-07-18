import { calculate } from "./calculator.js";
import { UnknownProvider } from "./errors.js";
import type { QuoteRequest } from "./models.js";
import { PayPalProvider, type PayPalCore } from "./providers/paypal/provider.js";

export class PaymentFeeEngine {
  private readonly _providers = new Map<string, PayPalProvider>();

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
      // TODO
    }
    return engine;
  }

  static async fromDocuments(args: {
    paypal?: PayPalCore | { core?: PayPalCore };
    stripe?: unknown;
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    await Promise.resolve();
    const engine = new PaymentFeeEngine();
    if (args.paypal) {
      const core = (args.paypal as { core?: PayPalCore }).core ?? (args.paypal as PayPalCore);
      engine.register("paypal", new PayPalProvider(core));
    }
    if (args.stripe) {
      // TODO
    }
    return engine;
  }

  register(provider: string, instance: PayPalProvider): void {
    this._providers.set(provider, instance);
  }

  quote(request: QuoteRequest): Record<string, unknown> {
    this.requireProvider(request.provider);

    if (request.provider === "paypal") {
      const paypalRequest = request;
      const provider = this._providers.get("paypal")!;
      const rules = provider.compileRules(paypalRequest);
      const result = calculate(paypalRequest.amount, paypalRequest.amount.currency, rules);

      return {
        provider: "paypal",
        status: "exact_for_public_rate",
        amount: result.amount,
        processing_fee: result.processing_fee,
        net_amount: result.net_amount,
        components: result.components,
        matched_rules: result.matched_rules,
        selected_product_id: paypalRequest.transaction.product_id,
        selected_variant_id: paypalRequest.transaction.variant_id,
        assumptions: [
          "Public standard pricing was used; negotiated merchant pricing is not represented.",
          "The published dataset does not encode provider settlement rounding, so standard currency rounding is used.",
        ],
        warnings: [],
        data: {
          provider: "paypal",
          schema_version: 1,
          market: paypalRequest.account_country,
          content_sha256: null,
          source_urls: [],
          source_updated_at: null,
          data_ref: "documents",
        },
      };
    }

    throw new Error("Stripe provider is not yet implemented.");
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
    return {};
  }

  private requireProvider(provider: string): void {
    if (!this._providers.has(provider)) {
      throw new UnknownProvider(provider);
    }
  }
}
