import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { DecimalMoney } from "../src/money.js";

void describe("DecimalMoney", async () => {
  await it("normalizes currency to uppercase", () => {
    const money = new DecimalMoney("100.00", "eur");
    assert.equal(money.value, "100");
    assert.equal(money.currency, "EUR");
  });

  await it("rejects negative values", () => {
    assert.throws(() => new DecimalMoney("-1", "USD"), RangeError);
  });
});
