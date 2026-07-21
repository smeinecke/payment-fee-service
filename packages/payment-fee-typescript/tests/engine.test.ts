import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { PaymentFeeEngine } from "../src/engine.js";
import {
  UnknownProvider,
  InsufficientTransactionContext,
  UnknownMarket,
} from "../src/errors.js";
import type { QuoteResult } from "../src/calculator.js";
import type { QuoteRequest } from "../src/models.js";

type EngineResult = QuoteResult & {
  provider: string;
  status: string;
  selected_product_id?: string | null;
  selected_variant_id?: string | null;
};

const paypalCore = {
  schema_version: 1,
  countries: [
    {
      country_code: "DE",
      derived: {
        status: "calculable",
        transaction_fee_rules: [
          {
            id: "other_commercial",
            variant_id: "standard",
            label: "Commercial transaction",
            percentage: "2.49",
            fixed_fee_schedule: "fixed__applies_to_markets=DE",
            international_surcharge_schedule: "surcharge__applies_to_markets=DE",
            calculation_status: "calculable",
            fee_components: [
              { type: "percentage" },
              { type: "fixed_fee_schedule" },
              { type: "international_surcharge_schedule" },
            ],
            conditions: {} as Record<string, unknown>,
          },
        ],
        fixed_fee_schedules: {
          "fixed__applies_to_markets=DE": { entries: { EUR: "0.35" } },
        },
        international_surcharge_schedules: {
          "surcharge__applies_to_markets=DE": {
            entries: [
              { payer_region: "US", percentage_points: "1.5" },
              {
                payer_region: "GB",
                percentage_points: "1.0",
                fixed_amount: "0.10",
                fixed_currency: "EUR",
              },
            ],
          },
        },
        maximum_fee_schedules: {},
      },
    },
  ],
};

const stripeCore = {
  schema_version: 1,
  markets: [
    {
      account_country: "US",
      rules: [
        {
          rule_id: "stripe:US:card:base",
          provider: "stripe",
          account_country: "US",
          classification_status: "calculable_rule",
          behavior: "base",
          product_id: "payment",
          variant_id: "card",
          payment_method: "card",
          label: "Card payment",
          unit: "per_transaction",
          conditions: [],
          fee_components: [
            { type: "percentage", value: "2.9" },
            { type: "fixed_amount", amount: "0.30", currency: "USD" },
          ],
        },
        {
          rule_id: "stripe:US:card:manual",
          provider: "stripe",
          account_country: "US",
          classification_status: "calculable_rule",
          behavior: "additive",
          product_id: "payment",
          variant_id: "card",
          payment_method: "card",
          label: "Manually entered card surcharge",
          unit: "per_transaction",
          conditions: [{ dimension: "card_entry_mode", operator: "eq", value: "manual" }],
          fee_components: [{ type: "percentage_surcharge", value: "0.5" }],
        },
      ],
    },
  ],
};

void describe("PaymentFeeEngine", async () => {
  await it("quotes PayPal without surcharge", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ paypal: paypalCore });
    const result = engine.quote({
      provider: "paypal",
      amount: { value: "100.00", currency: "EUR" },
      account_country: "DE",
      transaction: {
        product_id: "other_commercial",
        variant_id: "standard",
        transaction_region: "domestic",
      },
    }) as unknown as EngineResult;
    assert.equal(result.provider, "paypal");
    assert.equal(result.processing_fee.value, "2.84");
    assert.equal(result.net_amount.value, "97.16");
  });

  await it("quotes PayPal with surcharge", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ paypal: paypalCore });
    const result = engine.quote({
      provider: "paypal",
      amount: { value: "100.00", currency: "EUR" },
      account_country: "DE",
      transaction: {
        product_id: "other_commercial",
        variant_id: "standard",
        transaction_region: "international",
        payer_region: "US",
      },
    }) as unknown as EngineResult;
    assert.equal(result.processing_fee.value, "4.34");
    assert.equal(result.net_amount.value, "95.66");
    assert.equal(result.components[1].type, "surcharge");
  });

  await it("throws for missing PayPal payer region", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ paypal: paypalCore });
    assert.throws(
      () =>
        engine.quote({
          provider: "paypal",
          amount: { value: "100.00", currency: "EUR" },
          account_country: "DE",
          transaction: {
            product_id: "other_commercial",
            variant_id: "standard",
            transaction_region: "international",
          },
        }),
      InsufficientTransactionContext,
    );
  });

  await it("quotes Stripe base", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ stripe: stripeCore });
    const result = engine.quote({
      provider: "stripe",
      amount: { value: "100.00", currency: "USD" },
      account_country: "US",
      transaction: {
        product_id: "payment",
        variant_id: "card",
        payment_method: "card",
        channel: "online",
        card: { origin: "domestic", region: "domestic" },
      },
    }) as unknown as EngineResult;
    assert.equal(result.provider, "stripe");
    assert.equal(result.processing_fee.value, "3.20");
    assert.equal(result.net_amount.value, "96.80");
    assert.equal(result.status, "exact_for_public_rate");
  });

  await it("quotes Stripe with additive", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ stripe: stripeCore });
    const result = engine.quote({
      provider: "stripe",
      amount: { value: "100.00", currency: "USD" },
      account_country: "US",
      transaction: {
        product_id: "payment",
        variant_id: "card",
        payment_method: "card",
        channel: "online",
        card: { origin: "domestic", region: "domestic", entry_mode: "manual" },
      },
    }) as unknown as EngineResult;
    assert.equal(result.processing_fee.value, "3.70");
    assert.equal(result.net_amount.value, "96.30");
    assert.equal(result.components.length, 2);
    assert.equal(result.components[1].type, "surcharge");
  });

  await it("throws for unknown provider", () => {
    const engine = new PaymentFeeEngine();
    assert.throws(
      () =>
        engine.quote({
          provider: "braintree",
          amount: { value: "100.00", currency: "USD" },
          account_country: "US",
          transaction: { product_id: "payment", variant_id: "card" },
        } as unknown as QuoteRequest),
      UnknownProvider,
    );
  });

  await it("throws for missing Stripe market", async () => {
    const engine = await PaymentFeeEngine.fromDocuments({ stripe: stripeCore });
    assert.throws(
      () =>
        engine.quote({
          provider: "stripe",
          amount: { value: "100.00", currency: "USD" },
          account_country: "CA",
          transaction: {
            product_id: "payment",
            variant_id: "card",
            payment_method: "card",
          },
        }),
      UnknownMarket,
    );
  });
});
