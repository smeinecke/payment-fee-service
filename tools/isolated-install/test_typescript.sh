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

const engine = await PaymentFeeEngine.fromDocuments({
  paypal: {
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
                calculation_status: "calculable",
                fee_components: [
                  { type: "percentage", value: "2.49" },
                  { type: "fixed_fee_schedule", schedule_id: "fixed__applies_to_markets=DE" },
                ],
              },
            ],
            fixed_fee_schedules: {
              "fixed__applies_to_markets=DE": {
                entries: { EUR: "0.35" },
              },
            },
          },
        },
      ],
    },
  },
});

const result = engine.quote({
  provider: "paypal",
  amount: { value: "100.00", currency: "EUR" },
  account_country: "DE",
  transaction: {
    product_id: "other_commercial",
    variant_id: "standard",
    transaction_region: "domestic",
  },
});

if (result.processing_fee.value !== "2.84") {
  process.stderr.write(`Unexpected processing fee: ${result.processing_fee.value}\n`);
  process.exit(1);
}

console.log("TypeScript isolated install OK");
JS
node smoke.mjs
