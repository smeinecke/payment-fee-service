import { readFile, readdir } from "node:fs/promises";
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
  try {
    const engine = await PaymentFeeEngine.fromDocuments({
      paypal: providerDocuments.paypal,
      stripe: providerDocuments.stripe,
    });
    const actual = engine.quote(caseData.request);
    const expected = caseData.expected_result;
    if (!deepEqual(normalize(actual), normalize(expected))) {
      return { id: caseData.id, status: "mismatch", field: "result", actual: normalize(actual), expected: normalize(expected) };
    }
    return { id: caseData.id, status: "ok" };
  } catch (e) {
    const expectedError = caseData.expected_error;
    const actualError = { code: e.code, message: e.message, details: e.details || {} };
    if (!deepEqual(normalize(actualError), normalize(expectedError))) {
      return { id: caseData.id, status: "mismatch", field: "error", actual: normalize(actualError), expected: expectedError };
    }
    return { id: caseData.id, status: "ok" };
  }
}

async function main() {
  const manifest = JSON.parse(await readFile(join(CONFORMANCE_DIR, "manifest.json"), "utf-8"));
  const failures = [];
  for (const casePath of manifest.cases) {
    const caseData = JSON.parse(await readFile(join(CONFORMANCE_DIR, casePath), "utf-8"));
    const result = await runCase(caseData);
    console.log(`${result.id}: ${result.status}`);
    if (result.status !== "ok") failures.push(result);
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
