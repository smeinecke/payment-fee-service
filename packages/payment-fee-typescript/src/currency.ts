let currencies: Record<string, { minor_units: number } | undefined> = {
  EUR: { minor_units: 2 },
  JPY: { minor_units: 0 },
  KWD: { minor_units: 3 },
  USD: { minor_units: 2 },
};

export function minorUnits(currency: string): number {
  return currencies[currency]?.minor_units ?? 2;
}

export function setCurrencies(data: Record<string, { minor_units: number } | undefined>): void {
  currencies = data;
}

export function getCurrencies(): Record<string, { minor_units: number } | undefined> {
  return currencies;
}
