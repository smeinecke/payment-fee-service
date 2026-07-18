import { UnknownProvider } from "./errors.js";
import type { QuoteRequest } from "./models.js";

/**
 * Native TypeScript payment-fee calculation engine.
 *
 * @todo Implement PayPal and Stripe provider adapters.
 */
export class PaymentFeeEngine {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private readonly providers = new Map<string, any>();

  static async fromPaths(_args: {
    paypal?: string;
    stripe?: string;
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    // TODO: load providers from filesystem
    return new PaymentFeeEngine();
  }

  static async fromDocuments(_args: {
    paypal?: unknown;
    stripe?: unknown;
    validate?: boolean;
  }): Promise<PaymentFeeEngine> {
    // TODO: load providers from in-memory documents
    return new PaymentFeeEngine();
  }

  quote(_request: QuoteRequest): Record<string, unknown> {
    this.requireProvider(_request.provider);
    throw new Error("Provider adapters are not yet implemented.");
  }

  providers(): string[] {
    return [...this.providers.keys()];
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
    if (!this.providers.has(provider)) {
      throw new UnknownProvider(provider);
    }
  }
}
