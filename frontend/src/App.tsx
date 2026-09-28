import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  connectLive,
  get,
  post,
  type Activity,
  type Backtest,
  type ScanState,
  type Score,
  type Status,
  type Trade,
} from "./api";
import { ActivityCard } from "./components/Activity";
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
import { ScannerCard } from "./components/Scanner";

interface Toast {
  text: string;
  bad?: boolean;
}

export function App() {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
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
  const scan = useQuery({
    queryKey: ["scan"],
    queryFn: () => get<ScanState>("/api/scan"),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 1_500 : false),
  });

  // Eventos en tiempo real: cada tipo refresca solo lo que cambia.
  useEffect(
    () =>
      connectLive((message) => {
        const refresh = (...keys: string[]) =>
          keys.forEach((key) => void queryClient.invalidateQueries({ queryKey: [key] }));
        switch (message.type) {
          case "bar":
            refresh("status", "scores", "bars");
            break;
          case "signal":
          case "order":
            refresh("activity", "status");
            break;
          case "fill":
            refresh("activity", "status", "trades", "bars", "equity");
            break;
          case "state":
            refresh("status");
            break;
          case "scan":
            refresh("scan");
            break;
          case "hello":
            refresh("status");
            break;
        }
      }, setConnected),
    [queryClient],
  );

  const markets = status.data?.markets ?? [];
  const venues = status.data?.venues ?? [];
  const selectedMarket = market ?? markets[0]?.market ?? null;
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
    <div className="app">
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
          No se puede conectar con invert.io. ¿Está arrancado? Ejecuta <code>uv run invertio run</code>.
        </div>
      )}
      {status.data && !running && (
        <div className="banner" role="status">
          Motor parado: estás viendo el historial en solo lectura. Para vigilar los mercados en vivo
          ejecuta <code>uv run invertio run</code>.
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
          </div>
          {selectedMarket ? (
            <PriceChart key={selectedMarket} market={selectedMarket} />
          ) : (
            <p className="empty">No hay mercados configurados.</p>
          )}
        </section>
        <div className="stack">
          <ScoresCard scores={scores.data} running={running} />
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
