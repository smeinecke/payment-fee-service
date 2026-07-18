import { QuoteNotAvailable } from "../../errors.js";

interface FixedFeeSchedule {
  entries: Record<string, string | undefined>;
}

interface SurchargeSchedule {
  entries: {
    payer_region: string;
    percentage_points?: string | null;
  }[];
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

  fixed(scheduleId: string, currency: string): string {
    const schedule = this.derived.fixed_fee_schedules?.[scheduleId];
    if (schedule === undefined) {
      throw new QuoteNotAvailable("The selected PayPal fee category has no fixed-fee schedule.", {
        schedule_id: scheduleId,
      });
    }
    const value = schedule.entries[currency];
    if (value === undefined) {
      throw new QuoteNotAvailable(
        "No PayPal fixed fee is published for the transaction currency.",
        { schedule_id: scheduleId, currency },
      );
    }
    return value;
  }

  maximum(scheduleId: string, currency: string): string {
    const schedule = this.derived.maximum_fee_schedules?.[scheduleId];
    if (schedule === undefined) {
      throw new QuoteNotAvailable("The selected PayPal fee category has no maximum-fee schedule.", {
        schedule_id: scheduleId,
      });
    }
    const value = schedule.entries[currency];
    if (value === undefined) {
      throw new QuoteNotAvailable(
        "No PayPal maximum fee is published for the transaction currency.",
        { schedule_id: scheduleId, currency },
      );
    }
    return value;
  }

  surchargeRate(scheduleId: string, payerRegion: string | null | undefined): string | null {
    if (!payerRegion) return null;
    const schedule = this.derived.international_surcharge_schedules?.[scheduleId];
    if (schedule === undefined) return null;
    for (const entry of schedule.entries) {
      if (entry.payer_region.toLowerCase() === payerRegion.toLowerCase()) {
        return entry.percentage_points ?? null;
      }
    }
    return null;
  }
}
