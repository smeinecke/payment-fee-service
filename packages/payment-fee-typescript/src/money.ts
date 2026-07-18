import { Decimal } from "decimal.js";

export interface MoneyLike {
  value: string;
  currency: string;
}

export class DecimalMoney implements MoneyLike {
  readonly value: string;
  readonly currency: string;
  readonly decimal: Decimal;

  constructor(value: string, currency: string) {
    this.decimal = new Decimal(value);
    if (this.decimal.isNegative()) {
      throw new RangeError("Money value must be non-negative");
    }
    this.value = this.decimal.toFixed();
    this.currency = currency.toUpperCase();
  }

  static from(input: MoneyLike): DecimalMoney {
    return new DecimalMoney(input.value, input.currency);
  }

  toJSON(): MoneyLike {
    return { value: this.value, currency: this.currency };
  }
}
