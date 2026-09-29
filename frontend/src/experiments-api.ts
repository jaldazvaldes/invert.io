import { get } from "./api";
import type { SimulationConfig, SimulationDecision, SimulationEquity, SimulationStats, SimulationTrade } from "./simulation-api";

export type ExperimentState = "starting" | "running" | "waiting_data" | "error" | "stopped";
export type ExperimentTrial = "costs" | "original" | "timeframes" | "lab";

export interface MakerOrder {
  id: string;
  symbol: string;
  placed_at: string;
  expires_at: string;
  limit_price: string;
  quantity: string;
  reserved_eur: string;
  status: "pending" | "filled" | "expired" | "cancelled";
  finished_at?: string | null;
  filled_at?: string | null;
  reason?: string | null;
  incomplete?: boolean;
}

export interface ExperimentStrategy {
  id: string;
  name: string;
  description: string;
  rules: string[];
  sources: { title: string; url: string }[];
  execution?: "maker_entry" | "taker";
  timeframe_minutes?: number;
  base_strategy?: string;
  btc_filter?: number | null;
  lab_result?: { run: string; median_return_pct: number; positive_pct: number; trades_per_coin: number; median_trade_pct: number; buy_and_hold_pct: number };
  native_timeframe?: string;
  cooldown_minutes?: number;
  pending_minutes?: number;
  model_limitations?: string;
  config: SimulationConfig;
  started_at: string;
  last_cycle_at: string | null;
  state: ExperimentState;
  rank: number | null;
  stats: SimulationStats & {
    net_pnl_eur: string | null;
    fees_eur: string;
    max_drawdown_pct: string | null;
    profit_factor: string | null;
    win_rate_pct: string | null;
    closed_gross_eur?: string;
    closed_fees_eur?: string;
    reserved_eur?: string;
    pending_orders?: number;
  };
  positions: Record<string, SimulationTrade>;
  trades: SimulationTrade[];
  decisions: SimulationDecision[];
  equity: SimulationEquity[];
  pending_orders?: Record<string, MakerOrder>;
  maker_orders?: MakerOrder[];
}

export interface ExperimentsStatus {
  available: boolean;
  running: boolean;
  state: ExperimentState;
  last_error: string | null;
  last_cycle_at: string | null;
  started_at: string | null;
  experiment_id?: string;
  version?: number | string;
  name?: string;
  market_count?: number;
  cycles?: number;
  reference?: { name: string; initial_eur: string; equity_eur: string; return_pct: string };
  strategies?: ExperimentStrategy[];
  /** Solo en el grupo de 10 min y 1 h: mercados con velas largas al día por vela nativa. */
  history_ready?: Record<string, number>;
  data_errors?: number;
}

const API_PATHS: Record<ExperimentTrial, string> = { costs: "/api/execution-experiment", original: "/api/experiments", timeframes: "/api/timeframe-experiment", lab: "/api/lab-experiment" };
export const experimentApiPath = (trial: ExperimentTrial) => API_PATHS[trial];
export const getExperimentsStatus = (trial: ExperimentTrial = "original") => get<ExperimentsStatus>(`${experimentApiPath(trial)}/status`);
