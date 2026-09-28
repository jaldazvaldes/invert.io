import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ago, money, pct, points, price, qty, when } from "../format";
import { getSimulationStatus, type SimulationEquity, type SimulationTrade } from "../simulation-api";

const STATES = {
  running: { label: "Simulación en marcha", tone: "ok" },
  waiting_data: { label: "Esperando datos fiables", tone: "warn" },
  stopped: { label: "Simulación detenida", tone: "" },
  starting: { label: "Preparando simulación", tone: "warn" },
  error: { label: "Error de simulación", tone: "bad" },
};
const TRADE_STATES = { open: "Abierta", closed: "Cerrada", waiting_data: "Esperando datos" };
const ACTIONS = { buy: "Compra ficticia", sell: "Venta ficticia", skip: "Descartada", wait: "En espera" };
const REASONS: Record<string, string> = {
  target: "Objetivo observado", take_profit: "Objetivo observado", stop: "Stop observado", stop_loss: "Stop observado",
  score: "Puntuación de salida", expired: "Caducidad", capacity: "Cupo de posiciones completo",
  max_positions: "Cupo de posiciones completo", insufficient_cash: "Saldo ficticio insuficiente",
  insufficient_funds: "Saldo ficticio insuficiente", waiting_data: "Esperando datos fiables",
};
const reason = (value: string | null | undefined) => value ? REASONS[value] ?? value : "—";
const asNumber = (value: string | null | undefined): number | null => {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const eur = (value: string | null | undefined, signed = false) => {
  const number = asNumber(value);
  return number == null ? "—" : money(number, "EUR", signed);
};
const rate = (value: string | null | undefined) => {
  const number = asNumber(value);
  return number == null ? "—" : pct(number);
};
const valueTone = (value: string | null | undefined) => {
  const number = asNumber(value);
  return number == null || number === 0 ? "" : number > 0 ? "up" : "down";
};

/** Null valuations split the line: unknown account values are never replaced with zero. */
function SimulationCurve({ entries, initial }: { entries: SimulationEquity[]; initial: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const resize = new ResizeObserver(([entry]) => setWidth(Math.max(280, entry.contentRect.width)));
    resize.observe(element);
    return () => resize.disconnect();
  }, []);
  const data = useMemo(() => entries.map((entry) => ({ ...entry, timestamp: Date.parse(entry.at), value: asNumber(entry.equity_eur) }))
    .filter((entry) => Number.isFinite(entry.timestamp)).sort((a, b) => a.timestamp - b.timestamp), [entries]);
  const initialValue = asNumber(initial) ?? 50;
  const known = data.filter((entry) => entry.value != null);
  const minValue = Math.min(initialValue, ...known.map((entry) => entry.value!));
  const maxValue = Math.max(initialValue, ...known.map((entry) => entry.value!));
  const margin = Math.max((maxValue - minValue) * 0.15, 0.05);
  const low = minValue - margin;
  const high = maxValue + margin;
  const left = 58, right = width - 16, top = 16, bottom = 218;
  const start = data[0]?.timestamp ?? 0;
  const end = data.at(-1)?.timestamp ?? start;
  const x = (timestamp: number) => end === start ? (left + right) / 2 : left + (timestamp - start) / (end - start) * (right - left);
  const y = (value: number) => bottom - (value - low) / (high - low) * (bottom - top);
  const segments: { x: number; y: number }[][] = [];
  let segment: { x: number; y: number }[] = [];
  for (const entry of data) {
    if (entry.value == null) {
      if (segment.length) segments.push(segment);
      segment = [];
    } else segment.push({ x: x(entry.timestamp), y: y(entry.value) });
  }
  if (segment.length) segments.push(segment);
  const hovered = selected == null ? data.at(-1) : data[Math.min(selected, data.length - 1)];
  return <div ref={container} className="simulation-equity">
    {!known.length ? <p className="empty">Aún no hay valoraciones fiables para dibujar la curva.</p> : <svg
      viewBox={`0 0 ${width} 256`} role="img" tabIndex={0}
      aria-label="Evolución del valor de la cuenta ficticia. Usa las flechas para consultar observaciones. Los huecos corresponden a datos sin valoración fiable."
      onPointerMove={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        const pointerX = (event.clientX - box.left) * width / box.width;
        let closest = 0;
        data.forEach((entry, index) => { if (Math.abs(x(entry.timestamp) - pointerX) < Math.abs(x(data[closest].timestamp) - pointerX)) closest = index; });
        setSelected(closest);
      }}
      onPointerLeave={() => setSelected(null)}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          setSelected(Math.max(0, Math.min(data.length - 1, (selected ?? data.length - 1) + (event.key === "ArrowLeft" ? -1 : 1))));
        }
      }}>
      <title>Capital ficticio observado</title>
      {[low, (low + high) / 2, high].map((value) => <g key={value}><line x1={left} x2={right} y1={y(value)} y2={y(value)} className="simulation-gridline" /><text x={left - 8} y={y(value) + 4} textAnchor="end">{money(value, "EUR")}</text></g>)}
      <line x1={left} x2={right} y1={y(initialValue)} y2={y(initialValue)} className="simulation-baseline" />
      {segments.map((part, index) => part.length === 1 ? <circle key={index} cx={part[0].x} cy={part[0].y} r={3} className="simulation-point" /> : <path key={index} d={part.map((point, position) => `${position === 0 ? "M" : "L"}${point.x},${point.y}`).join(" ")} className="simulation-line" />)}
      {hovered && <line x1={x(hovered.timestamp)} x2={x(hovered.timestamp)} y1={top} y2={bottom} className="simulation-crosshair" />}
      <text x={left} y={243}>{when(data[0]?.at)}</text><text x={right} y={243} textAnchor="end">{data.length > 1 ? when(data.at(-1)?.at) : ""}</text>
    </svg>}
    <div className="simulation-curve-caption"><span className="card-note">Línea discontinua: capital inicial · Los huecos no equivalen a pérdidas.</span>{hovered && <span className="num">{when(hovered.at)} · {hovered.value == null ? "Sin valoración fiable" : money(hovered.value, "EUR")}</span>}</div>
  </div>;
}

function PositionState({ trade }: { trade: SimulationTrade }) {
  return <><span className={`pill ${trade.status === "waiting_data" ? "warn" : trade.status === "open" ? "ok" : ""}`}>{TRADE_STATES[trade.status]}</span>{trade.had_data_gap && <span className="cell-sub">Con interrupción de datos</span>}</>;
}

export function SimulationPanel() {
  const query = useQuery({ queryKey: ["simulation", "status"], queryFn: getSimulationStatus, refetchInterval: 5_000, retry: false });
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 10_000); return () => window.clearInterval(timer); }, []);
  const data = query.data;
  const config = data?.config;
  const stats = data?.stats;
  const initial = config?.initial_eur ?? stats?.initial_eur ?? "50";
  const state = STATES[query.isError ? "error" : data?.state ?? "starting"];
  const positions = Object.values(data?.positions ?? {});
  const history = [...new Map([...(data?.trades ?? []), ...positions].map((trade) => [trade.id, trade])).values()].sort((a, b) => b.opened_at.localeCompare(a.opened_at));
  const decisions = [...(data?.decisions ?? [])].sort((a, b) => b.at.localeCompare(a.at));
  const equity = data?.equity ?? [];
  const valuationMissing = !!data?.available && stats?.equity_eur == null;

  return <main className="simulation-panel">
    <header className="topbar simulation-topbar"><div className="brand"><span className="brand-mark" aria-hidden="true">↗</span>invert.io</div><div className="topbar-status"><span className="pill brand">SIMULACIÓN AUTOMÁTICA</span><span className={`pill ${state.tone}`}><span className="dot" />{state.label}</span><span className="pill">Revolut X · EUR · 1 min</span></div><span className="simulation-update card-note">Último ciclo: {ago(data?.last_cycle_at, now)}</span></header>
    <div className="simulation-intro"><div><p className="simulation-eyebrow">Cuenta ficticia del algoritmo</p><h1>{eur(initial)} ficticios</h1><p className="muted">Compras y ventas simuladas según las señales, con un máximo de {config?.max_positions ?? 5} posiciones abiertas.</p></div><span className="pill brand">No usa tu saldo real</span></div>
    <p className="simulation-explanation">Se ejecuta cada minuto con libros observados; stop y objetivo no garantizan precio. Todo es ficticio: no envía órdenes a Revolut.</p>
    {query.isPending && <p className="empty" role="status">Consultando la cuenta ficticia…</p>}
    {query.isError && <div className="banner bad" role="alert"><span>No se pudo consultar la simulación: {query.error.message}</span><button className="btn" onClick={() => void query.refetch()}>Reintentar</button></div>}
    {data?.last_error && <p className="banner bad" role="alert">{data.last_error}</p>}
    {data && !data.available && <p className="banner" role="status"><span>La simulación todavía no tiene una cuenta ficticia preparada. Se activa al arrancar <code>invertio analyze --simulate</code>. Tiene su propio estado, independiente del paper clásico.</span></p>}
    {data?.available && !data.running && <p className="banner" role="status">La simulación automática está detenida. Puedes consultar su historial.</p>}
    {valuationMissing && <p className="banner" role="status">Faltan datos fiables para valorar todas las posiciones. El valor de la cuenta y el resultado sin realizar se muestran sin estimación hasta recuperar datos.</p>}

    <div className="kpis simulation-kpis">
      <section className="card"><div className="kpi-label">Saldo ficticio libre</div><div className="kpi-value">{eur(stats?.cash_eur ?? data?.cash_eur)}</div><div className="kpi-sub">Asignación por entrada: {eur(config?.allocation_eur ?? "10")} con comisión</div></section>
      <section className="card"><div className="kpi-label">Valor de la cuenta ficticia</div><div className="kpi-value">{eur(stats?.equity_eur)}</div><div className="kpi-sub">{valuationMissing ? "Sin valoración fiable" : `Rendimiento observado: ${rate(stats?.return_pct)}`}</div></section>
      <section className="card"><div className="kpi-label">Resultado realizado, neto de costes</div><div className={`kpi-value ${valueTone(stats?.realized_pnl_eur)}`}>{eur(stats?.realized_pnl_eur, true)}</div><div className="kpi-sub">{stats?.closed_trades ?? 0} operaciones cerradas · {stats?.wins ?? 0} positivas · {stats?.losses ?? 0} negativas</div></section>
      <section className="card"><div className="kpi-label">Posiciones abiertas</div><div className="kpi-value">{stats?.open_positions ?? positions.length}<small> / {config?.max_positions ?? 5}</small></div><div className="kpi-sub">{stats?.waiting_positions ?? positions.filter((trade) => trade.status === "waiting_data").length} esperando datos · Sin realizar: {eur(stats?.unrealized_pnl_eur, true)}</div></section>
    </div>
    <section className="card" aria-labelledby="simulation-curve-title"><div className="card-head"><h2 id="simulation-curve-title">Evolución de la cuenta ficticia</h2><span className="card-note">Desde {when(data?.started_at)} · Últimas {equity.length} observaciones</span></div><SimulationCurve entries={equity} initial={initial} /></section>

    <section className="card section" aria-labelledby="simulation-positions-title"><div className="card-head"><h2 id="simulation-positions-title">Posiciones ficticias abiertas</h2><span className="card-note">Niveles fijados al simular la entrada</span></div>
      {!positions.length ? <p className="empty">No hay posiciones ficticias abiertas. El algoritmo espera señales que cumplan las reglas y dispongan de saldo y cupo.</p> : <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Mercado</th><th>Entrada simulada</th><th>Cantidad</th><th>Stop / objetivo</th><th>Comisión de entrada</th><th>Estado</th></tr></thead><tbody>{positions.map((trade) => <tr key={trade.id}><td><strong>{trade.symbol}</strong><span className="cell-sub">{when(trade.opened_at)}</span></td><td className="num">{price(asNumber(trade.entry))} €</td><td className="num">{qty(Number(trade.quantity))}</td><td className="num">{price(asNumber(trade.stop))} €<span className="cell-sub">{price(asNumber(trade.target))} €</span></td><td className="num">{eur(trade.entry_fee_eur)}</td><td><PositionState trade={trade} />{trade.reason && <span className="cell-sub">{reason(trade.reason)}</span>}</td></tr>)}</tbody></table></div>}
    </section>

    <div className="grid grid-half section">
      <section className="card" aria-labelledby="simulation-rules-title"><div className="card-head"><h2 id="simulation-rules-title">Reglas de esta simulación</h2></div><dl className="simulation-rules">
        <div><dt>Compra simulada</dt><dd>Nota ≥ {points(Number(config?.entry_score ?? 70))}</dd></div><div><dt>Venta por señal</dt><dd>Nota ≤ {points(Number(config?.exit_score ?? 40))}</dd></div><div><dt>Stop / objetivo</dt><dd>{config?.stop_atr ?? "2"} ATR / {config?.target_atr ?? "3"} ATR</dd></div><div><dt>Caducidad</dt><dd>{config?.lifetime_minutes ?? 240} minutos</dd></div><div><dt>Máximo de posiciones</dt><dd>{config?.max_positions ?? 5}</dd></div><div><dt>Asignación por compra</dt><dd>{eur(config?.allocation_eur ?? "10")} con comisión</dd></div><div><dt>Comisión por lado</dt><dd>{config?.fee_pct ?? "0.09"} %</dd></div><div><dt>Spread / deslizamiento máximo</dt><dd>{config?.max_spread_pct ?? "0.3"} % / {config?.max_slippage_pct ?? "0.1"} %</dd></div>
      </dl><p className="card-note">Los costes del libro se reflejan en los precios simulados y las comisiones se descuentan del saldo ficticio. Una interrupción de datos queda señalada en la posición.</p></section>
      <section className="card" aria-labelledby="simulation-decisions-title"><div className="card-head"><h2 id="simulation-decisions-title">Decisiones del algoritmo</h2><span className="card-note">Últimas {decisions.length} decisiones</span></div>{!decisions.length ? <p className="empty">Aún no hay decisiones. Aquí aparecerán las entradas, salidas y motivos para descartar o esperar.</p> : <div className="simulation-decisions">{decisions.map((decision, index) => <article key={`${decision.at}-${decision.opportunity_id}-${index}`}><div><strong>{decision.symbol}</strong><span className={`pill ${decision.action === "skip" || decision.action === "wait" ? "warn" : ""}`}>{ACTIONS[decision.action]}</span><time className="card-note" dateTime={decision.at}>{when(decision.at)}</time></div><p>{reason(decision.reason)}</p></article>)}</div>}</section>
    </div>

    <section className="card section" aria-labelledby="simulation-history-title"><div className="card-head"><h2 id="simulation-history-title">Entradas y salidas ficticias</h2><a className="btn simulation-export" href="/api/simulation/export.csv" download>Exportar CSV</a></div><p className="card-note">Se muestran las posiciones abiertas y hasta 200 operaciones históricas. El CSV incluye el historial completo.</p>
      {!history.length ? <p className="empty">Todavía no se ha simulado ninguna operación.</p> : <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Mercado / fechas</th><th>Estado</th><th>Cantidad</th><th>Entrada / salida</th><th>Comisiones entrada / salida</th><th className="r">Resultado neto</th><th>Motivo</th></tr></thead><tbody>{history.map((trade) => <tr key={trade.id}><td><strong>{trade.symbol}</strong><span className="cell-sub">Entrada: {when(trade.opened_at)}</span><span className="cell-sub">Salida: {when(trade.closed_at)}</span></td><td><PositionState trade={trade} /></td><td className="num">{qty(Number(trade.quantity))}</td><td className="num">{price(asNumber(trade.entry))} €<span className="cell-sub">{trade.exit_price == null ? "—" : `${price(asNumber(trade.exit_price))} €`}</span></td><td className="num">{eur(trade.entry_fee_eur)}<span className="cell-sub">{eur(trade.exit_fee_eur)}</span></td><td className={`r num ${valueTone(trade.pnl_eur)}`}>{eur(trade.pnl_eur, true)}</td><td className="simulation-reason">{reason(trade.reason)}</td></tr>)}</tbody></table></div>}
    </section>
  </main>;
}
