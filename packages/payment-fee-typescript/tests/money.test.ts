import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { DecimalMoney } from "../src/money.js";

describe("DecimalMoney", () => {
  it("normalizes currency to uppercase", () => {
    const money = new DecimalMoney("100.00", "eur");
    assert.equal(money.value, "100.00");
    assert.equal(money.currency, "EUR");
  });

  it("rejects negative values", () => {
    assert.throws(() => new DecimalMoney("-1", "USD"), RangeError);
  });
});
