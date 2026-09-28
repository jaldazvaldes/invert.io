import { get } from "./api";

export interface SimulationConfig {
  initial_eur: string;
  allocation_eur: string;
  fee_pct: string;
  max_slippage_pct: string;
  max_spread_pct: string;
  stop_atr: string;
  target_atr: string;
  max_positions: number;
  quote_max_age_seconds: number;
  bar_max_age_seconds: number;
  lifetime_minutes: number;
  entry_score: string | number;
  exit_score: string | number;
}

export interface SimulationTrade {
  id: string;
  symbol: string;
  status: "open" | "closed" | "waiting_data";
  opened_at: string;
  closed_at: string | null;
  entry: string;
  quantity: string;
  stop: string;
  target: string;
  entry_fee_eur: string;
  investment_eur?: string;
  exit_fee_eur: string | null;
  exit_price: string | null;
  pnl_eur: string | null;
  reason: string | null;
  had_data_gap: boolean;
}

export interface SimulationDecision {
  at: string;
  opportunity_id: string;
  symbol: string;
  action: "buy" | "sell" | "skip" | "wait";
  reason: string;
}

export interface SimulationEquity {
  at: string;
  cash_eur: string;
  equity_eur: string | null;
}

export interface SimulationStats {
  initial_eur: string;
  cash_eur: string;
  equity_eur: string | null;
  realized_pnl_eur: string;
  unrealized_pnl_eur: string | null;
  return_pct: string | null;
  open_positions: number;
  waiting_positions: number;
  closed_trades: number;
  wins: number;
  losses: number;
}

export interface SimulationStatus {
  available: boolean;
  running: boolean;
  state: "running" | "waiting_data" | "stopped" | "starting" | "error";
  last_error: string | null;
  version?: number;
  config?: Partial<SimulationConfig>;
  started_at?: string | null;
  last_cycle_at?: string | null;
  cash_eur?: string;
  positions?: Record<string, SimulationTrade>;
  trades?: SimulationTrade[];
  decisions?: SimulationDecision[];
  equity?: SimulationEquity[];
  stats?: Partial<SimulationStats>;
}

export const getSimulationStatus = () => get<SimulationStatus>("/api/simulation/status");
