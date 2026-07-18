import type { MoneyLike } from "./money.js";

export interface CardContext {
  origin?: string | null;
  region?: string | null;
  type?: string | null;
  network?: string | null;
  tier?: string | null;
  entry_mode?: string | null;
}

export interface SettlementContext {
  currency?: string | null;
  timing?: string | null;
}

export interface BankContext {
  validation?: string | null;
  transfer_type?: string | null;
}

export interface BaseTransaction {
  product_id?: string | null;
  variant_id?: string | null;
  payment_method?: string | null;
  payment_method_variant?: string | null;
  channel?: string | null;
  pricing_plan?: string | null;
  pricing_tier?: string | null;
  payer?: string | null;
  unit?: string | null;
  currency_conversion_required?: boolean | null;
  context?: Record<string, unknown>;
}

export interface PayPalTransaction extends BaseTransaction {
  transaction_region?: string | null;
  payer_region?: string | null;
  surcharge_region?: string | null;
  merchant_approval_required?: boolean | null;
  withdrawal_method?: string | null;
  authorization_channel?: string | null;
  point_of_sale?: boolean | null;
  card_present?: boolean | null;
  transaction_purpose?: string | null;
  funding_source?: string | null;
  service?: string | null;
  recipient_location?: string | null;
  volume_status?: string | null;
  fee_currency?: string | null;
}

export interface StripeTransaction extends BaseTransaction {
  card?: CardContext | null;
  settlement?: SettlementContext | null;
  bank?: BankContext | null;
  recurring?: boolean | null;
  billing_type?: string | null;
  transaction_region?: string | null;
  customer_country?: string | null;
  presentment_currency?: string | null;
  settlement_currency?: string | null;
  settlement_timing?: string | null;
  bank_account_validation?: string | null;
  integration_type?: string | null;
  product_feature?: string | null;
  contract_length?: string | null;
  cross_border?: boolean | null;
  feature_enabled?: string | null;
  dispute_state?: string | null;
  card_tier?: string | null;
  card_type?: string | null;
  card_network?: string | null;
  card_origin?: string | null;
  card_region?: string | null;
  card_entry_mode?: string | null;
}

export interface BaseQuoteRequest {
  provider: string;
  amount: MoneyLike;
  account_country: string;
  customer_country?: string | null;
  settlement_currency?: string | null;
  transaction: BaseTransaction;
}

export interface PayPalQuoteRequest extends BaseQuoteRequest {
  provider: "paypal";
  transaction: PayPalTransaction;
}

export interface StripeQuoteRequest extends BaseQuoteRequest {
  provider: "stripe";
  transaction: StripeTransaction;
}

export type QuoteRequest = PayPalQuoteRequest | StripeQuoteRequest;
