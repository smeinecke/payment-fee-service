import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function loadCurrencyTable(): Record<string, { minor_units: number } | undefined> {
  const root = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    join(root, "../../../contracts/currencies.json"),
    join(root, "../contracts/currencies.json"),
  ];
  for (const path of candidates) {
    try {
      const data = JSON.parse(readFileSync(path, "utf-8")) as Record<
        string,
        { minor_units: number }
      >;
      return data;
    } catch {
      // ignore
    }
  }
  return {
    EUR: { minor_units: 2 },
    JPY: { minor_units: 0 },
    KWD: { minor_units: 3 },
    USD: { minor_units: 2 },
  };
}

let currencies: Record<string, { minor_units: number } | undefined> = loadCurrencyTable();

export function minorUnits(currency: string): number {
  return currencies[currency]?.minor_units ?? 2;
}

export function setCurrencies(data: Record<string, { minor_units: number } | undefined>): void {
  currencies = data;
}

export function getCurrencies(): Record<string, { minor_units: number } | undefined> {
  return currencies;
}
