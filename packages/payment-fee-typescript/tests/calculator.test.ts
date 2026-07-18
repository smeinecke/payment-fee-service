import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { calculate } from "../src/calculator.js";

void describe("Calculator", async () => {
  await it("calculates percentage plus fixed", () => {
    const result = calculate({ value: "100.00", currency: "USD" }, "USD", [
      {
        rule_id: "r1",
        label: "2.9% + 30¢",
        behavior: "base",
        percentage: "2.9",
        fixed_amount: "0.30",
        fixed_currency: "USD",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
    ]);
    assert.equal(result.processing_fee.value, "3.20");
    assert.equal(result.net_amount.value, "96.80");
  });

  await it("applies minimum fee", () => {
    const result = calculate({ value: "5.00", currency: "USD" }, "USD", [
      {
        rule_id: "r1",
        label: "2% with minimum",
        behavior: "base",
        percentage: "2.0",
        minimum_amount: "0.50",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
    ]);
    assert.equal(result.processing_fee.value, "0.50");
    assert.equal(result.components[0].minimum_applied, true);
  });

  await it("applies maximum fee", () => {
    const result = calculate({ value: "1000.00", currency: "USD" }, "USD", [
      {
        rule_id: "r1",
        label: "2% with cap",
        behavior: "base",
        percentage: "2.0",
        maximum_amount: "5.00",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
    ]);
    assert.equal(result.processing_fee.value, "5.00");
    assert.equal(result.components[0].maximum_applied, true);
  });

  await it("adds surcharge to base fee", () => {
    const result = calculate({ value: "100.00", currency: "USD" }, "USD", [
      {
        rule_id: "r1",
        label: "2.9% + 30¢",
        behavior: "base",
        percentage: "2.9",
        fixed_amount: "0.30",
        fixed_currency: "USD",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
      {
        rule_id: "r2",
        label: "Surcharge",
        component_type: "surcharge",
        behavior: "additive",
        percentage: "0.5",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
    ]);
    assert.equal(result.processing_fee.value, "3.70");
    assert.equal(result.net_amount.value, "96.30");
    assert.equal(result.components[1].type, "surcharge");
  });

  await it("rounds zero-decimal currency", () => {
    const result = calculate({ value: "1000", currency: "JPY" }, "JPY", [
      {
        rule_id: "r1",
        label: "3.6%",
        behavior: "base",
        percentage: "3.6",
        classification_status: "calculable_rule",
        exactness: "exact",
      },
    ]);
    assert.equal(result.processing_fee.value, "36");
    assert.equal(result.processing_fee.currency, "JPY");
  });
});
