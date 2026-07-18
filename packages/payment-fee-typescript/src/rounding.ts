import { Decimal } from "decimal.js";
import { minorUnits } from "./currency.js";

Decimal.set({ rounding: Decimal.ROUND_HALF_UP });

export function quantum(minorUnits: number): Decimal {
  switch (minorUnits) {
    case 0:
      return new Decimal("1");
    case 3:
      return new Decimal("0.001");
    default:
      return new Decimal("0.01");
  }
}

export function roundMoney(value: Decimal, currency: string): Decimal {
  const q = quantum(minorUnits(currency));
  return value.dividedBy(q).toDecimalPlaces(0, Decimal.ROUND_HALF_UP).times(q);
}

export function toMoneyString(value: Decimal, currency: string): string {
  const m = minorUnits(currency);
  const fixed = value.toDecimalPlaces(m, Decimal.ROUND_HALF_UP).toFixed(m);
  if (fixed === "-0" || fixed === "-0." + "0".repeat(m)) {
    return "0" + (m > 0 ? "." + "0".repeat(m) : "");
  }
  return fixed;
}

export function toDecimal(value: unknown): Decimal {
  if (value instanceof Decimal) return value;
  return new Decimal(String(value));
}
