import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  ApiError,
  connectLive,
  get,
  post,
  type Activity,
  type AnalysisStatus,
  type AnalysisSummary,
  type Backtest,
  type LabSummary,
  type ScanState,
  type Score,
  type Status,
  type Trade,
} from "./api";
import { ActivityCard } from "./components/Activity";
import { AnalysisDashboard } from "./components/Analysis";
import {
  BacktestsCard,
  MarketsCard,
  PositionsCard,
  ScoresCard,
  TradesCard,
  VenueCards,
} from "./components/Cards";
import { EquityChart, PriceChart } from "./components/Charts";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { Header } from "./components/Header";
import { LabCard } from "./components/Lab";
import { ScannerCard } from "./components/Scanner";

const ManualTradingPanel = lazy(() => import("./components/ManualTrading").then((module) => ({ default: module.ManualTradingPanel })));
const SimulationPanel = lazy(() => import("./components/SimulationPanel").then((module) => ({ default: module.SimulationPanel })));
const ExperimentsPanel = lazy(() => import("./components/ExperimentsPanel").then((module) => ({ default: module.ExperimentsPanel })));

interface Toast {
  text: string;
  bad?: boolean;
}

export function App() {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [view, setView] = useState<"analysis" | "simulation" | "experiments" | "manual" | "paper" | null>(null);
  const analysisStatus = useQuery({
    queryKey: ["analysis", "status"],
    queryFn: () => get<AnalysisStatus>("/api/analysis/status"),
    refetchInterval: 5_000,
    retry: false,
  });
  const analysisSummary = useQuery({
    queryKey: ["analysis", "summary", "all"],
    queryFn: () => get<AnalysisSummary>("/api/analysis/summary"),
    refetchInterval: 60_000,
    retry: false,
  });
  const activeView = view ?? (analysisStatus.data?.available || analysisSummary.data?.total ? "analysis" : "paper");

  useEffect(
    () => connectLive((message) => {
      const refresh = (...keys: string[]) =>
        keys.forEach((key) => void queryClient.invalidateQueries({ queryKey: [key] }));
      switch (message.type) {
        case "analysis": refresh("analysis", "simulation", "experiments"); break;
        case "bar": refresh("status", "scores", "bars"); break;
        case "signal":
        case "order": refresh("activity", "status"); break;
        case "fill": refresh("activity", "status", "trades", "bars", "equity"); break;
        case "state": refresh("status"); break;
        case "scan": refresh("scan"); break;
        case "hello": refresh("status", "analysis", "simulation", "experiments"); break;
      }
    }, setConnected),
    [queryClient],
  );

  return <div className="app">
    <nav className="workspace-tabs tabs" aria-label="Modo de invert.io">
      <button className={`tab ${activeView === "analysis" ? "active" : ""}`} aria-current={activeView === "analysis" ? "page" : undefined} onClick={() => setView("analysis")}>Análisis</button>
      <button className={`tab ${activeView === "simulation" ? "active" : ""}`} aria-current={activeView === "simulation" ? "page" : undefined} onClick={() => setView("simulation")}>Simulación</button>
      <button className={`tab ${activeView === "experiments" ? "active" : ""}`} aria-current={activeView === "experiments" ? "page" : undefined} onClick={() => setView("experiments")}>Estrategias</button>
      <button className={`tab ${activeView === "manual" ? "active" : ""}`} aria-current={activeView === "manual" ? "page" : undefined} onClick={() => setView("manual")}>Operar</button>
      <button className={`tab ${activeView === "paper" ? "active" : ""}`} aria-current={activeView === "paper" ? "page" : undefined} onClick={() => setView("paper")}>Paper y laboratorio</button>
    </nav>
    {!view && (analysisStatus.isPending || analysisSummary.isPending) ? <p className="empty" role="status">Conectando con invert.io…</p> :
      activeView === "analysis" ? <AnalysisDashboard status={analysisStatus.data} statusError={analysisStatus.error} connected={connected} /> : activeView === "simulation" ? <Suspense fallback={<p className="empty" role="status">Cargando simulación…</p>}><SimulationPanel /></Suspense> : activeView === "experiments" ? <Suspense fallback={<p className="empty" role="status">Cargando estrategias…</p>}><ExperimentsPanel /></Suspense> : activeView === "manual" ? <Suspense fallback={<p className="empty" role="status">Cargando operaciones manuales…</p>}><ManualTradingPanel /></Suspense> : <PaperDashboard connected={connected} onSimulation={() => setView("simulation")} />}
  </div>;
}

function PaperDashboard({ connected, onSimulation }: { connected: boolean; onSimulation: () => void }) {
  const queryClient = useQueryClient();
  const [market, setMarket] = useState<string | null>(null);
  const [equityVenue, setEquityVenue] = useState<string | null>(null);
  const [confirmPanic, setConfirmPanic] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);

  const status = useQuery({
    queryKey: ["status"],
    queryFn: () => get<Status>("/api/status"),
    refetchInterval: 5_000,
  });
  const running = status.data?.running ?? false;
  const scores = useQuery({
    queryKey: ["scores"],
    queryFn: () => get<Score[]>("/api/scores"),
    refetchInterval: 15_000,
    enabled: running,
  });
  const activity = useQuery({
    queryKey: ["activity"],
    queryFn: () => get<Activity[]>("/api/activity?limit=100"),
    refetchInterval: 20_000,
  });
  const trades = useQuery({
    queryKey: ["trades"],
    queryFn: () => get<Trade[]>("/api/trades?limit=100"),
    refetchInterval: 60_000,
  });
  const backtests = useQuery({
    queryKey: ["backtests"],
    queryFn: () => get<Backtest[]>("/api/backtests"),
    refetchInterval: 120_000,
  });
  const lab = useQuery({
    queryKey: ["lab"],
    queryFn: () => get<LabSummary | null>("/api/lab"),
    refetchInterval: 60_000,
  });
  const scan = useQuery({
    queryKey: ["scan"],
    queryFn: () => get<ScanState>("/api/scan"),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1_500 : false),
  });

  const markets = status.data?.markets ?? [];
  const venues = status.data?.venues ?? [];
  // Por defecto: una moneda con posición abierta; si no hay, BTC; si no, la primera.
  const openPosition = venues.flatMap((v) => v.positions)[0]?.market;
  const selectedMarket =
    market ??
    openPosition ??
    markets.find((m) => m.market.endsWith(":BTC/EUR"))?.market ??
    markets[0]?.market ??
    null;
  const selectedVenue = equityVenue ?? venues[0]?.venue ?? null;

  const notify = useCallback((text: string, bad = false) => {
    setToast({ text, bad });
    window.setTimeout(() => setToast(null), 4000);
  }, []);

  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post<unknown>(path, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (error) =>
      notify(error instanceof ApiError ? error.message : "No se pudo completar la acción", true),
  });

  const startScan = (timeframe: string, all: boolean) =>
    action.mutate(
      { path: "/api/scan", body: { timeframe, all_markets: all } },
      { onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["scan"] }) },
    );

  return (
    <div>
      <Header
        status={status.data}
        connected={connected}
        busy={action.isPending}
        onPause={() =>
          action.mutate({ path: "/api/control/pause" }, { onSuccess: () => notify("Entradas en pausa") })
        }
        onResume={() =>
          action.mutate({ path: "/api/control/resume" }, { onSuccess: () => notify("Motor en marcha") })
        }
        onPanic={() => setConfirmPanic(true)}
      />

      {status.isError && (
        <div className="banner bad" role="alert">
          No se puede consultar el simulador clásico. Comprueba la conexión con invert.io.
        </div>
      )}
      {status.data && !running && (
        <div className="banner" role="status">
          <span>Este es el historial del simulador clásico. La recogida de Análisis y la nueva Simulación automática tienen su propio estado.</span>
          <button className="btn" onClick={onSimulation}>Ir a Simulación</button>
        </div>
      )}
      {status.data?.state === "halted" && (
        <div className="banner bad" role="status">
          Motor detenido por el botón de pánico: no abrirá posiciones hasta que pulses «Reanudar».
        </div>
      )}

      {status.data && <VenueCards status={status.data} />}

      <div className="grid grid-main">
        <section className="card" aria-labelledby="chart-title">
          <div className="card-head">
            <h2 id="chart-title">Gráfico</h2>
            {markets.length > 8 ? (
              <select
                className="select"
                aria-label="Mercado del gráfico"
                value={selectedMarket ?? ""}
                onChange={(event) => setMarket(event.target.value)}
              >
                {[...markets]
                  .sort((a, b) => a.market.localeCompare(b.market))
                  .map((m) => (
                    <option key={m.market} value={m.market}>
                      {m.market.split(":")[1]}
                    </option>
                  ))}
              </select>
            ) : (
              <div className="tabs" role="tablist" aria-label="Mercado">
                {markets.map((m) => (
                  <button
                    key={m.market}
                    className="tab"
                    role="tab"
                    aria-selected={m.market === selectedMarket}
                    onClick={() => setMarket(m.market)}
                  >
                    {m.market.split(":")[1]}
                  </button>
                ))}
              </div>
            )}
          </div>
          {selectedMarket ? (
            <PriceChart key={selectedMarket} market={selectedMarket} />
          ) : (
            <p className="empty">No hay mercados configurados.</p>
          )}
        </section>
        <div className="stack">
          <ScoresCard
            scores={scores.data}
            running={running}
            onSelect={(m) => {
              setMarket(m);
              document.getElementById("chart-title")?.scrollIntoView({ behavior: "smooth" });
            }}
          />
          <MarketsCard
            markets={markets}
            running={running}
            onToggle={(m, enabled) =>
              action.mutate(
                { path: "/api/markets/toggle", body: { market: m, enabled } },
                {
                  onSuccess: () =>
                    notify(enabled ? `${m} activado` : `${m} desactivado: no abrirá posiciones`),
                },
              )
            }
          />
        </div>
      </div>

      <div className="section">{status.data && <PositionsCard status={status.data} />}</div>
      <div className="section">
        <ActivityCard items={activity.data} />
      </div>

      <div className="grid grid-half section">
        <TradesCard trades={trades.data} />
        <section className="card" aria-labelledby="equity-title">
          <div className="card-head">
            <h2 id="equity-title">Evolución del capital</h2>
            <div className="tabs" role="tablist" aria-label="Venue">
              {venues.map((v) => (
                <button
                  key={v.venue}
                  className="tab"
                  role="tab"
                  aria-selected={v.venue === selectedVenue}
                  onClick={() => setEquityVenue(v.venue)}
                >
                  {v.venue}
                </button>
              ))}
            </div>
          </div>
          {selectedVenue ? (
            <EquityChart key={selectedVenue} venue={selectedVenue} />
          ) : (
            <p className="empty">Sin datos.</p>
          )}
        </section>
      </div>

      <div className="section">
        <ScannerCard scan={scan.data} onStart={startScan} starting={action.isPending} />
      </div>
      <div className="section">
        <LabCard lab={lab.data} />
      </div>
      <div className="section">
        <BacktestsCard backtests={backtests.data} />
      </div>

      {confirmPanic && (
        <ConfirmDialog
          title="¿Activar el pánico?"
          message="Se cancelan todas las órdenes, se cierran todas las posiciones al precio actual y el motor deja de abrir posiciones hasta que pulses «Reanudar»."
          confirmLabel="Sí, cerrar todo"
          onCancel={() => setConfirmPanic(false)}
          onConfirm={() => {
            setConfirmPanic(false);
            action.mutate(
              { path: "/api/control/panic", body: { confirm: true } },
              { onSuccess: () => notify("Pánico ejecutado: todo cerrado", true) },
            );
          }}
        />
      )}
      {toast && (
        <div className={`toast ${toast.bad ? "bad" : ""}`} role="status" aria-live="polite">
          {toast.text}
        </div>
      )}
    </div>
  );
}
