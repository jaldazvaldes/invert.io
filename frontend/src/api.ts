// Tipos y llamadas a la API del motor (src/invertio/api/app.py).

export type EngineState = "running" | "paused" | "halted";

export interface Position {
  market: string;
  symbol: string;
  quantity: number;
  avg_price: number;
  last_price: number | null;
  unrealized: number;
  unrealized_pct: number;
  stop_loss: number | null;
  take_profit: number | null;
  strategy: string;
}

export interface VenueStatus {
  venue: string;
  currency: string;
  equity: number;
  cash: number;
  initial: number;
  return_pct: number;
  positions: Position[];
}

export interface MarketStatus {
  market: string;
  strategy: string;
  enabled: boolean;
  last_bar: string | null;
  stale: boolean;
}

export interface Today {
  trades: number;
  wins: number;
  pnl: Record<string, number>;
  signals: number;
  approved: number;
  rejections: [string, number][];
}

export interface Status {
  running: boolean;
  mode: string;
  state: EngineState | null;
  timeframe: string;
  server_time: string;
  venues: VenueStatus[];
  markets: MarketStatus[];
  today: Today | null;
}

export interface Score {
  market: string;
  venue: string;
  symbol: string;
  ts: string;
  close: number;
  score: number;
  points: Record<string, number>;
  max_points: Record<string, number>;
  factors: string[];
  atr_pct: number;
  tradable: boolean;
  stop_loss: number;
  take_profit: number | null;
  meets: boolean;
}

export interface ScanNote {
  market: string;
  note: string;
}

export type ScanRow = Score | ScanNote;

export interface ScanState {
  status: "idle" | "running" | "done" | "error";
  timeframe: string | null;
  all_markets: boolean;
  progress: Record<string, [number, number]>;
  started_at: string | null;
  finished_at: string | null;
  rows: ScanRow[];
  skipped: { venue: string; reason: string }[];
  error: string | null;
}

export interface SignalActivity {
  kind: "signal";
  ts: string;
  market: string;
  strategy: string;
  action: "buy" | "close";
  price: number;
  stop_loss: number | null;
  take_profit: number | null;
  reason: string;
  approved: boolean | null;
  decision: string | null;
  order_status: string | null;
  order_type: string | null;
  fill_price: number | null;
}

export interface FillActivity {
  kind: "fill";
  ts: string;
  market: string;
  strategy: string;
  side: "buy" | "sell";
  quantity: number;
  price: number;
  fee: number;
  currency: string;
  reason: string;
}

export type Activity = SignalActivity | FillActivity;

export interface Trade {
  market: string;
  currency: string;
  strategy: string;
  entry_time: string;
  exit_time: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  fees: number;
  return_pct: number;
  exit_reason: string;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface BarsResponse {
  market: string;
  timeframe: string;
  tf_seconds: number;
  bars: Candle[];
  fills: { ts: string; side: "buy" | "sell"; price: number; quantity: number; reason: string }[];
  position: {
    quantity: number;
    avg_price: number;
    stop_loss: number | null;
    take_profit: number | null;
  } | null;
}

export interface EquityPoint {
  ts: string;
  equity: number;
}

export interface BacktestMarket {
  market: string;
  timeframe: string | null;
  from: string | null;
  to: string | null;
  total_return_pct: number;
  buy_and_hold_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  trades: number;
  win_rate_pct: number;
  profit_factor: number | null;
  fees_paid: number;
}

export interface Backtest {
  id: string;
  strategy: string;
  created_at: string;
  report_url: string;
  markets: BacktestMarket[];
}

export interface LabAggregate {
  symbols: number;
  median_return: number;
  pct_positive: number;
  median_buy_and_hold: number;
  median_drawdown: number;
  avg_trades: number;
  median_trade_pct: number;
}

export interface LabCandidate {
  strategy: string;
  timeframe: string;
  params: Record<string, unknown>;
  train: LabAggregate;
  test: LabAggregate | null;
  verdict: string;
  reasons: string[];
}

export interface LabSummary {
  id: string;
  report_url: string;
  test_start: string;
  finished_at: string;
  symbols_train: number;
  symbols_test: number;
  backtests: number;
  candidates: LabCandidate[];
}

export interface AppConfig {
  mode: string;
  timeframe: string;
  risk: Record<string, number | string | boolean>;
  venues: { id: string; asset_class: string; quote: string; symbols: string[] }[];
  markets: { market: string; strategy: string }[];
}

export interface AnalysisStatus {
  available: boolean;
  running: boolean;
  state: "starting" | "running" | "stopped" | "degraded";
  timeframe: string;
  interval_seconds: number;
  selected: string[];
  coverage?: {
    scope: "top" | "all_eur";
    catalog_eur: number;
    observed: number;
    entry_eligible: number;
    excluded: number;
    data_access: "public" | "authenticated";
    request_interval_seconds: number;
  };
  warmup: { ready: number; total: number };
  cycles: number;
  last_cycle_at: string | null;
  last_error: string | null;
  telegram: { configured: boolean; pending: number; sent: number; failed: number };
  config: Record<string, unknown>;
}

export interface AnalysisCosts {
  eligible: boolean;
  reasons: string[];
  entry: number | null;
  stop: number | null;
  target: number | null;
  quantity: number | null;
  round_trip_cost_pct: number | null;
  round_trip_cost_eur: number | null;
  buy_slippage_pct: number | null;
  sell_slippage_pct: number | null;
  target_net_pct: number | null;
}

export interface AnalysisObservation {
  market: string;
  bar_ts: string | null;
  observed_at: string;
  score: number | null;
  points: Record<string, number>;
  max_points: number | Record<string, number>;
  positive: string[];
  negative: string[];
  reasons: string[];
  state: "warming" | "watching" | "eligible" | "open" | "unavailable";
  price: number | null;
  atr_pct: number | null;
  spread_pct: number | null;
  quote_volume: number | null;
  quote_ts?: string | null;
  costs: AnalysisCosts | null;
  opportunity_id: string | null;
}

export interface AnalysisRanking {
  rows: AnalysisObservation[];
  excluded: { market: string; reasons: string[] }[];
  updated_at: string | null;
}

export interface AnalysisHorizon {
  at: string | null;
  return_pct: number | null;
  net_return_pct: number | null;
  quality: string;
}

export interface AnalysisOpportunity {
  id: string;
  market: string;
  created_at: string;
  updated_at: string;
  bar_ts: string;
  status: "open" | "closed" | "interrupted";
  outcome: string | null;
  quality: "complete" | "incomplete" | "ambiguous";
  ended_at: string | null;
  entry: number;
  stop: number;
  target: number;
  reference_eur: number;
  quantity: number;
  score: number;
  points: Record<string, number>;
  max_points: number | Record<string, number>;
  positive: string[];
  negative: string[];
  costs: AnalysisCosts;
  rule_version: string;
  config: Record<string, unknown>;
  high_water: number;
  low_water: number;
  mfe_pct: number | null;
  mae_pct: number | null;
  horizons: Partial<Record<"15m" | "1h" | "4h", AnalysisHorizon>>;
  end_price: number | null;
  estimated_return_pct: number | null;
  reason: string | null;
}

export interface AnalysisNotification {
  id?: string;
  kind: string;
  status: string;
  created_at?: string;
  sent_at?: string | null;
  last_error?: string | null;
}

export interface AnalysisDetail extends AnalysisOpportunity {
  observations: AnalysisObservation[];
  notifications: AnalysisNotification[];
}

export interface AnalysisSummary {
  total: number;
  open: number;
  closed: number;
  interrupted: number;
  complete: number;
  incomplete: number;
  ambiguous: number;
  positive: number;
  known_results: number;
  mean_return_pct: number | null;
  outcomes: Record<string, number>;
  versions?: Record<string, number>;
  by_version?: Record<string, AnalysisSummary>;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

let tokenPromise: Promise<string> | null = null;

function sessionToken(): Promise<string> {
  tokenPromise ??= get<{ token: string }>("/api/session").then((s) => s.token);
  return tokenPromise;
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // cuerpo no JSON
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { headers: { Accept: "application/json" } }));
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const token = await sessionToken();
  return parse<T>(
    await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Invertio-Token": token },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export type LiveMessage =
  | { type: "hello"; running: boolean }
  | { type: "bar"; market: string; close_time: string; close: number }
  | { type: "signal"; market: string; action: string; approved: boolean; reason: string }
  | { type: "order"; market: string; side: string; status: string }
  | { type: "fill"; market: string; side: string; price: number; quantity: number }
  | { type: "state"; state: EngineState; reason: string }
  | { type: "scan"; status: string }
  | { type: "analysis"; state?: string };

/** WebSocket con reconexión automática. Devuelve la función para cerrarlo. */
export function connectLive(
  onMessage: (message: LiveMessage) => void,
  onConnection: (connected: boolean) => void,
): () => void {
  let socket: WebSocket | null = null;
  let closed = false;
  let retry = 1000;
  let timer: number | undefined;

  const open = () => {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${protocol}://${location.host}/api/ws`);
    socket.onopen = () => {
      retry = 1000;
      onConnection(true);
    };
    socket.onmessage = (event) => onMessage(JSON.parse(event.data as string) as LiveMessage);
    socket.onclose = () => {
      onConnection(false);
      if (!closed) {
        timer = window.setTimeout(open, retry);
        retry = Math.min(retry * 2, 15000);
      }
    };
  };
  open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
