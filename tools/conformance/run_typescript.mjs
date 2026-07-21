/**
 * Run the TypeScript implementation against the shared conformance suite.
 *
 * This harness is intentionally independent from run_python.py and run_php.php.
 * Sharing code between the three runners would risk a shared bug masking a real
 * cross-language divergence, which is the property the differential gate is
 * meant to catch.
 */

import { readFile, writeFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { PaymentFeeEngine } from "../../packages/payment-fee-typescript/dist/index.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CONFORMANCE_DIR = join(__dirname, "../../contracts/conformance");

function stripNulls(value) {
  if (value === null || value === undefined) return undefined;
  if (Array.isArray(value)) return value.map(stripNulls).filter((v) => v !== undefined);
  if (typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (v !== null && v !== undefined) out[k] = stripNulls(v);
    }
    return out;
  }
  return value;
}

function normalize(result) {
  return result === null || result === undefined ? null : stripNulls(result);
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    const sorted = {};
    for (const key of Object.keys(value).sort()) {
      sorted[key] = canonical(value[key]);
    }
    return sorted;
  }
  return value;
}

function deepEqual(a, b) {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

async function runCase(caseData) {
  const providerDocuments = caseData.provider_documents || {};
  let actual = null;
  let actualError = null;
  try {
    const engine = await PaymentFeeEngine.fromDocuments({
      paypal: providerDocuments.paypal,
      stripe: providerDocuments.stripe,
    });
    actual = engine.quote(caseData.request);
  } catch (e) {
    actualError = { code: e.code, message: e.message, details: e.details || {} };
  }

  const expected = caseData.expected_result;
  const expectedError = caseData.expected_error;

  let status = "ok";
  if (!deepEqual(normalize(actual), normalize(expected))) {
    status = "mismatch";
  }
  if (status === "ok" && !deepEqual(normalize(actualError), normalize(expectedError))) {
    status = "mismatch";
  }

  return {
    id: caseData.id,
    status,
    actual,
    expected,
    actual_error: actualError,
    expected_error: expectedError,
  };
}

async function main() {
  const emitIndex = process.argv.indexOf("--emit");
  const emitPath = emitIndex >= 0 ? process.argv[emitIndex + 1] : null;

  const manifest = JSON.parse(await readFile(join(CONFORMANCE_DIR, "manifest.json"), "utf-8"));
  const failures = [];
  const emitted = [];
  for (const casePath of manifest.cases) {
    const caseData = JSON.parse(await readFile(join(CONFORMANCE_DIR, casePath), "utf-8"));
    const result = await runCase(caseData);
    console.log(`${result.id}: ${result.status}`);
    emitted.push({ id: result.id, status: result.status, actual: result.actual, error: result.actual_error });
    if (result.status !== "ok") {
      failures.push({
        id: result.id,
        status: result.status,
        field: !deepEqual(normalize(result.actual), normalize(result.expected)) ? "result" : "error",
        actual: result.actual,
        expected: result.expected,
      });
    }
  }

  if (emitPath) {
    await writeFile(emitPath, JSON.stringify(emitted, null, 2));
  }

  if (failures.length > 0) {
    console.error("\nFailures:");
    for (const failure of failures) {
      console.error(JSON.stringify(failure, null, 2));
    }
    process.exit(1);
  }
  console.log("\nAll TypeScript conformance cases passed.");
}

main();
