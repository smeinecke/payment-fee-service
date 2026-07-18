export * from "./index.js";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setCurrencies } from "./currency.js";

function loadCurrencies(): void {
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
      setCurrencies(data);
      return;
    } catch {
      // ignore
    }
  }
}

loadCurrencies();
