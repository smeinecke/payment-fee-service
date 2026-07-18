#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TS_PACKAGE="$REPO_ROOT/packages/payment-fee-typescript"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

cd "$TS_PACKAGE"
npm run build
npm pack --pack-destination "$TMP_DIR" --silent

cd "$TMP_DIR"
TARBALL="$(echo ./*.tgz)"
npm install "$TARBALL" --silent

cat > smoke.mjs <<'JS'
import { PaymentFeeEngine } from "@smeinecke/payment-fee/node";

const paypalCore = {
  core: {
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
                { type: "percentage", value: "2.49" },
                { type: "fixed_fee_schedule" },
                { type: "international_surcharge_schedule" },
              ],
            },
          ],
          fixed_fee_schedules: {
            "fixed__applies_to_markets=DE": {
              entries: { EUR: "0.35" },
            },
          },
          international_surcharge_schedules: {
            "surcharge__applies_to_markets=DE": {
              entries: [{ payer_region: "US", percentage_points: "1.5" }],
            },
          },
        },
      },
    ],
  },
};

const stripeCore = {
  core: {
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
  },
};

const engine = await PaymentFeeEngine.fromDocuments({ paypal: paypalCore, stripe: stripeCore });

const paypalDomestic = engine.quote({
  provider: "paypal",
  amount: { value: "100.00", currency: "EUR" },
  account_country: "DE",
  transaction: {
    product_id: "other_commercial",
    variant_id: "standard",
    transaction_region: "domestic",
  },
});
if (paypalDomestic.processing_fee.value !== "2.84") {
  throw new Error(`PayPal domestic fee mismatch: ${paypalDomestic.processing_fee.value}`);
}

const paypalInternational = engine.quote({
  provider: "paypal",
  amount: { value: "100.00", currency: "EUR" },
  account_country: "DE",
  transaction: {
    product_id: "other_commercial",
    variant_id: "standard",
    transaction_region: "international",
    payer_region: "US",
  },
});
if (paypalInternational.processing_fee.value !== "4.34") {
  throw new Error(`PayPal international fee mismatch: ${paypalInternational.processing_fee.value}`);
}

let threw = false;
try {
  engine.quote({
    provider: "paypal",
    amount: { value: "100.00", currency: "EUR" },
    account_country: "DE",
    transaction: {
      product_id: "other_commercial",
      variant_id: "standard",
      transaction_region: "international",
    },
  });
} catch (e) {
  if (e.code === "INSUFFICIENT_TRANSACTION_CONTEXT") {
    threw = true;
  }
}
if (!threw) {
  throw new Error("Expected InsufficientTransactionContext for missing payer region");
}

const stripeBase = engine.quote({
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
});
if (stripeBase.processing_fee.value !== "3.20") {
  throw new Error(`Stripe base fee mismatch: ${stripeBase.processing_fee.value}`);
}

const stripeAdditive = engine.quote({
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
});
if (stripeAdditive.processing_fee.value !== "3.70") {
  throw new Error(`Stripe additive fee mismatch: ${stripeAdditive.processing_fee.value}`);
}

console.log("TypeScript isolated install OK");
JS
node smoke.mjs
