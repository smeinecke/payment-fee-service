import { QuoteNotAvailable } from "../../errors.js";

export interface SurchargeEntry {
  payer_region: string;
  percentage_points?: string | null;
  fixed_amount?: string | null;
  fixed_currency?: string | null;
}

interface FixedFeeSchedule {
  entries: Record<string, string | undefined>;
}

interface SurchargeSchedule {
  entries: SurchargeEntry[];
}

interface MaximumFeeSchedule {
  entries: Record<string, string | undefined>;
}

export interface PayPalDerived {
  fixed_fee_schedules?: Record<string, FixedFeeSchedule | undefined> | undefined;
  international_surcharge_schedules?: Record<string, SurchargeSchedule | undefined> | undefined;
  maximum_fee_schedules?: Record<string, MaximumFeeSchedule | undefined> | undefined;
}

export class ScheduleRegistry {
  constructor(private readonly derived: PayPalDerived) {}

  fixed(scheduleId: string, currency: string): string;
  fixed(scheduleId: string, currency: string, raiseOnMissing: false): string | undefined;
  fixed(scheduleId: string, currency: string, raiseOnMissing = true): string | undefined {
    const schedule = this.derived.fixed_fee_schedules?.[scheduleId];
    if (schedule === undefined) {
      if (raiseOnMissing) {
        throw new QuoteNotAvailable("The selected PayPal fee category has no fixed-fee schedule.", {
          schedule_id: scheduleId,
        });
      }
      return undefined;
    }
    const value = schedule.entries[currency];
    if (value === undefined) {
      if (raiseOnMissing) {
        throw new QuoteNotAvailable(
          "No PayPal fixed fee is published for the transaction currency.",
          { schedule_id: scheduleId, currency },
        );
      }
      return undefined;
    }
    return value;
  }

  maximum(scheduleId: string, currency: string): string;
  maximum(scheduleId: string, currency: string, raiseOnMissing: false): string | undefined;
  maximum(scheduleId: string, currency: string, raiseOnMissing = true): string | undefined {
    const schedule = this.derived.maximum_fee_schedules?.[scheduleId];
    if (schedule === undefined) {
      if (raiseOnMissing) {
        throw new QuoteNotAvailable(
          "The selected PayPal fee category has no maximum-fee schedule.",
          {
            schedule_id: scheduleId,
          },
        );
      }
      return undefined;
    }
    const value = schedule.entries[currency];
    if (value === undefined) {
      if (raiseOnMissing) {
        throw new QuoteNotAvailable(
          "No PayPal maximum fee is published for the transaction currency.",
          { schedule_id: scheduleId, currency },
        );
      }
      return undefined;
    }
    return value;
  }

  surchargeRegions(scheduleId: string): string[] {
    const schedule = this.derived.international_surcharge_schedules?.[scheduleId];
    if (schedule === undefined) return [];
    return schedule.entries.map((entry) => entry.payer_region);
  }

  surchargeRate(scheduleId: string, payerRegion: string): string | undefined {
    const schedule = this.derived.international_surcharge_schedules?.[scheduleId];
    if (schedule === undefined) {
      throw new QuoteNotAvailable(
        "The selected PayPal fee category has no international surcharge schedule.",
        { schedule_id: scheduleId },
      );
    }
    for (const entry of schedule.entries) {
      if (entry.payer_region.toLowerCase() === payerRegion.toLowerCase()) {
        return entry.percentage_points ?? undefined;
      }
    }
    return undefined;
  }

  surcharge(
    scheduleId: string,
    payerRegion: string | null | undefined,
    currency: string,
  ): {
    percentage?: string | null;
    fixed_amount?: string | null;
    fixed_currency?: string | null;
  } | null {
    if (!payerRegion) return null;
    const schedule = this.derived.international_surcharge_schedules?.[scheduleId];
    if (schedule === undefined) return null;
    for (const entry of schedule.entries) {
      if (entry.payer_region.toLowerCase() === payerRegion.toLowerCase()) {
        if (
          entry.fixed_amount &&
          (entry.fixed_currency ?? currency).toUpperCase() !== currency.toUpperCase()
        ) {
          throw new QuoteNotAvailable(
            "A PayPal international surcharge schedule uses a fixed amount in a different currency.",
            {
              schedule_id: scheduleId,
              currency,
              fixed_currency: entry.fixed_currency ?? currency,
            },
          );
        }
        return {
          percentage: entry.percentage_points ?? null,
          fixed_amount: entry.fixed_amount ?? null,
          fixed_currency: entry.fixed_currency ?? currency,
        };
      }
    }
    return null;
  }
}
