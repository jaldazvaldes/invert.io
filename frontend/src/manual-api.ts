import { get, post } from "./api";

// Monetary values and quantities remain decimal strings across the API boundary.
export type ManualOrderStatus =
  | "preview" | "submitting" | "unknown" | "pending"
  | "filled" | "cancelled" | "rejected" | "expired";

export interface ManualOrder {
  id: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: string;
  limit_price: string;
  notional_eur: string;
  fee_reserve_eur: string;
  max_debit_eur: string;
  estimated_proceeds_eur: string;
  created_at: string;
  expires_at: string;
  status: ManualOrderStatus;
  exchange_id: string | null;
  exchange_status: string | null;
  filled_quantity: string;
  reason: string | null;
}

export interface ManualStatus {
  available: boolean;
  configured: boolean;
  enabled: boolean;
  budget_eur: string;
  committed_eur: string;
  remaining_eur: string;
  balances_updated_at?: string | null;
  balances: { currency: string; available: string; reserved: string; total: string }[];
  positions: { symbol: string; quantity: string }[];
  orders: ManualOrder[];
  error: string | null;
}

export type ManualPreviewInput =
  | { symbol: string; side: "buy"; budget_eur: string }
  | { symbol: string; side: "sell"; quantity: string };

export class ManualRequestTimeout extends Error {
  constructor() {
    super("La solicitud ha tardado más de 30 segundos. Su resultado debe comprobarse en el historial.");
    this.name = "ManualRequestTimeout";
  }
}

/** No retries or aborted POSTs: an uncertain submission is reconciled by its original id. */
async function once<T>(request: Promise<T>): Promise<T> {
  let timer: number | undefined;
  try {
    return await Promise.race([
      request,
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => reject(new ManualRequestTimeout()), 30_000);
      }),
    ]);
  } finally {
    window.clearTimeout(timer);
  }
}

export const getManualStatus = () => get<ManualStatus>("/api/manual/status");
export const getManualMarkets = () => get<{ symbols: string[] }>("/api/manual/markets");
export const refreshManualStatus = () => once(post<ManualStatus>("/api/manual/refresh"));
export const previewManualOrder = (input: ManualPreviewInput) =>
  once(post<ManualOrder>("/api/manual/preview", input));
export const confirmManualOrder = (previewId: string) =>
  once(post<ManualOrder>("/api/manual/confirm", { preview_id: previewId, confirm: true }));
export const cancelManualOrder = (orderId: string) =>
  once(post<ManualOrder>("/api/manual/cancel", { order_id: orderId, confirm: true }));
