import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { valuesEqual } from "../src/providers/stripe/condition-matcher.js";

void describe("ConditionMatcher valuesEqual", async () => {
  await it("only matches booleans to booleans of the same value", () => {
    assert.equal(valuesEqual(true, true), true);
    assert.equal(valuesEqual(false, false), true);
    assert.equal(valuesEqual(true, false), false);
    assert.equal(valuesEqual(false, true), false);
    assert.equal(valuesEqual(true, 1), false);
    assert.equal(valuesEqual(false, 0), false);
    assert.equal(valuesEqual(1, true), false);
    assert.equal(valuesEqual(0, false), false);
    assert.equal(valuesEqual(true, "true"), false);
    assert.equal(valuesEqual(false, "false"), false);
    assert.equal(valuesEqual("true", true), false);
    assert.equal(valuesEqual("false", false), false);
  });

  await it("compares strings case-insensitively", () => {
    assert.equal(valuesEqual("USD", "usd"), true);
    assert.equal(valuesEqual("Domestic", "DOMESTIC"), true);
    assert.equal(valuesEqual("us", "eu"), false);
  });

  await it("coerces numeric values", () => {
    assert.equal(valuesEqual("2.90", 2.9), true);
    assert.equal(valuesEqual(10, "10.0"), true);
    assert.equal(valuesEqual("1.10", 1.1), true);
    assert.equal(valuesEqual("1", "2"), false);
  });
});
