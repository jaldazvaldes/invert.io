import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { experimentApiPath, getExperimentsStatus, type ExperimentStrategy, type ExperimentState, type ExperimentTrial, type MakerOrder } from "../experiments-api";
import { ago, money, pct, price, qty, when } from "../format";
import type { SimulationTrade } from "../simulation-api";

const STATES: Record<ExperimentState, { label: string; tone: string }> = {
  starting: { label: "Preparando estrategias", tone: "warn" },
  running: { label: "Comparación en marcha", tone: "ok" },
  waiting_data: { label: "Esperando datos fiables", tone: "warn" },
  error: { label: "Error de actualización", tone: "bad" },
  stopped: { label: "Comparación detenida", tone: "" },
};
const COLORS: Record<string, string> = { score_base: "#008d72", trend_ema: "#4268c4", breakout: "#ba680c", rsi_rebound: "#9251bc", rsi_1m_taker: "#008d72", rsi_1m_maker: "#4268c4", rsi_5m_taker: "#ba680c", rsi_5m_maker: "#9251bc", score_base_10m: "#008d72", trend_ema_10m: "#4268c4", breakout_10m: "#ba680c", rsi_rebound_10m: "#9251bc", score_base_1h: "#6cd3b3", trend_ema_1h: "#9db5f2", breakout_1h: "#e8ad63", rsi_rebound_1h: "#cf9fe8", score_4h: "#008d72", score_4h_btc50: "#4268c4", score_4h_btc200: "#9251bc", breakout_1h_btc50: "#ba680c", breakout_1h_btc200: "#c2410c", trailing_4h_btc50_maker: "#008d72", trailing_4h_btc50_taker: "#6cd3b3", breakout_1h_btc50_maker: "#ba680c", breakout_1h_btc50_taker: "#e8ad63", score_4h_btc50_maker: "#4268c4", score_4h_btc50_taker: "#9db5f2" };
const ACTIONS = { buy: "Compra ficticia", sell: "Venta ficticia", skip: "Descartada", wait: "En espera" };
const number = (value: string | null | undefined): number | null => {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const eur = (value: string | null | undefined, signed = false) => number(value) == null ? "—" : money(number(value)!, "EUR", signed);
const rate = (value: string | null | undefined, signed = true) => number(value) == null ? "—" : pct(number(value)!, signed);
const tone = (value: string | null | undefined) => number(value) == null || number(value) === 0 ? "" : number(value)! > 0 ? "up" : "down";
const color = (id: string) => COLORS[id] ?? "#68778c";
const duration = (minutes: number) => minutes >= 1440 && minutes % 1440 === 0 ? `${minutes / 1440} ${minutes === 1440 ? "día" : "días"}` : minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`;
const TRIALS: { id: ExperimentTrial; label: string; timeframes: string }[] = [
  { id: "costs", label: "Prueba de costes", timeframes: "1 y 5 min" },
  { id: "original", label: "Comparación original", timeframes: "1 min" },
  { id: "timeframes", label: "10 min y 1 hora", timeframes: "10 min y 1 h" },
  { id: "lab", label: "Del laboratorio", timeframes: "4 h y 1 h" },
  { id: "learned", label: "Aprendizajes", timeframes: "4 h y 1 h" },
];
const MAKER_STATES = { pending: "Pendiente", filled: "Ejecutada en simulación", expired: "Caducada", cancelled: "Cancelada" };

function MakerOrders({ strategy, exportUrl }: { strategy: ExperimentStrategy; exportUrl: string }) {
  const pending = Object.values(strategy.pending_orders ?? {}).sort((a, b) => a.placed_at.localeCompare(b.placed_at));
  const completed = [...(strategy.maker_orders ?? [])].sort((a, b) => (b.finished_at ?? b.placed_at).localeCompare(a.finished_at ?? a.placed_at));
  const row = (order: MakerOrder, isPending: boolean) => <tr key={order.id}>
    <td><strong>{order.symbol}</strong><span className="cell-sub">Creada: {when(order.placed_at)}</span><span className="cell-sub">{isPending ? `Caduca: ${when(order.expires_at)}` : `Final: ${when(order.finished_at)}`}</span>{order.incomplete && <span className="cell-sub">Observación incompleta por falta de datos</span>}</td>
    <td className="num">{price(number(order.limit_price))} €</td><td className="num">{qty(Number(order.quantity))}</td><td className="num">{eur(order.reserved_eur)}</td>
    <td><span className={`pill ${isPending ? "warn" : order.status === "filled" ? "ok" : ""}`}>{MAKER_STATES[order.status] ?? order.status}</span>{order.reason && <span className="cell-sub simulation-reason">{order.reason}</span>}</td>
  </tr>;
  const table = (orders: MakerOrder[], isPending: boolean) => <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Mercado / fechas</th><th>Precio límite</th><th>Cantidad</th><th>{isPending ? "Efectivo reservado" : "Reserva original"}</th><th>Estado / motivo</th></tr></thead><tbody>{orders.map((order) => row(order, isPending))}</tbody></table></div>;
  return <>
    <section className="card section" aria-labelledby="experiments-pending-title"><div className="card-head"><h2 id="experiments-pending-title">Compras maker pendientes de {strategy.name}</h2><span className="card-note">{pending.length} pendientes · {eur(strategy.stats.reserved_eur ?? "0")} reservados</span></div>
      <p className="card-note">Una orden pendiente reserva efectivo ficticio y cupo. Todavía no es una posición comprada; puede caducar o cancelarse sin ejecución.</p>
      {pending.length ? table(pending, true) : <p className="empty">No hay compras maker pendientes.</p>}
    </section>
    <section className="card section" aria-labelledby="experiments-maker-history-title"><div className="card-head"><h2 id="experiments-maker-history-title">Historial de órdenes maker</h2><span className="card-note">Últimas {completed.length} finalizadas</span><a className="btn simulation-export" href={exportUrl} download>Exportar órdenes CSV</a></div>
      <p className="card-note">Las caducadas o canceladas no cuentan como operaciones cerradas. Una ejecución de entrada aparece después en posiciones y, al venderse, en operaciones cerradas.</p>
      {completed.length ? table(completed, false) : <p className="empty">Todavía no hay órdenes maker finalizadas.</p>}
    </section>
  </>;
}

/** Each null splits its strategy line; missing values are never interpolated or replaced with zero. */
function ComparisonCurve({ strategies, initial }: { strategies: ExperimentStrategy[]; initial: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(800);
  const [selected, setSelected] = useState<number | null>(null);
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(280, entry.contentRect.width)));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const series = useMemo(() => strategies.map((strategy) => ({ id: strategy.id, name: strategy.name, data: strategy.equity.map((entry) => ({ timestamp: Date.parse(entry.at), value: number(entry.equity_eur) })).filter((entry) => Number.isFinite(entry.timestamp)).sort((a, b) => a.timestamp - b.timestamp) })), [strategies]);
  const timestamps = [...new Set(series.flatMap((item) => item.data.map((entry) => entry.timestamp)))].sort((a, b) => a - b);
  const baseline = number(initial) ?? 50;
  const values = series.flatMap((item) => item.data.flatMap((entry) => entry.value == null ? [] : [entry.value]));
  const min = Math.min(baseline, ...values), max = Math.max(baseline, ...values);
  const padding = Math.max((max - min) * 0.15, 0.05), low = min - padding, high = max + padding;
  const left = 60, right = width - 16, top = 16, bottom = 238;
  const start = timestamps[0] ?? 0, end = timestamps.at(-1) ?? start;
  const x = (timestamp: number) => end === start ? (left + right) / 2 : left + (timestamp - start) / (end - start) * (right - left);
  const y = (value: number) => bottom - (value - low) / (high - low) * (bottom - top);
  const selectedTime = timestamps[selected == null ? timestamps.length - 1 : Math.min(selected, timestamps.length - 1)];
  const paths = series.map((item) => {
    const segments: { timestamp: number; value: number }[][] = [];
    let segment: { timestamp: number; value: number }[] = [];
    for (const entry of item.data) {
      if (entry.value == null) { if (segment.length) segments.push(segment); segment = []; }
      else segment.push({ timestamp: entry.timestamp, value: entry.value });
    }
    if (segment.length) segments.push(segment);
    return { ...item, segments };
  });
  return <div ref={container} className="simulation-equity experiments-curve">
    {!values.length ? <p className="empty">Las curvas aparecerán al guardar las primeras valoraciones fiables.</p> : <svg viewBox={`0 0 ${width} 278`} role="img" tabIndex={0}
      aria-label="Comparación del capital ficticio de cada estrategia. Usa las flechas para consultar las observaciones. Los huecos significan que falta una valoración fiable."
      onPointerMove={(event) => { const rect = event.currentTarget.getBoundingClientRect(); const pointerX = (event.clientX - rect.left) * width / rect.width; let closest = 0; timestamps.forEach((timestamp, index) => { if (Math.abs(x(timestamp) - pointerX) < Math.abs(x(timestamps[closest]) - pointerX)) closest = index; }); setSelected(closest); }}
      onPointerLeave={() => setSelected(null)}
      onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); setSelected(Math.max(0, Math.min(timestamps.length - 1, (selected ?? timestamps.length - 1) + (event.key === "ArrowLeft" ? -1 : 1)))); } }}>
      <title>Estrategias desde el mismo capital inicial</title>
      {[low, (low + high) / 2, high].map((value) => <g key={value}><line x1={left} x2={right} y1={y(value)} y2={y(value)} className="simulation-gridline" /><text x={left - 8} y={y(value) + 4} textAnchor="end">{money(value, "EUR")}</text></g>)}
      <line x1={left} x2={right} y1={y(baseline)} y2={y(baseline)} className="simulation-baseline" />
      {paths.map((item) => <g key={item.id}>{item.segments.map((segment, index) => segment.length === 1 ? <circle key={index} cx={x(segment[0].timestamp)} cy={y(segment[0].value)} r={3} fill={color(item.id)} /> : <path key={index} d={segment.map((entry, position) => `${position ? "L" : "M"}${x(entry.timestamp)},${y(entry.value)}`).join(" ")} className="simulation-line" style={{ stroke: color(item.id) }} />)}</g>)}
      {selectedTime != null && <line x1={x(selectedTime)} x2={x(selectedTime)} y1={top} y2={bottom} className="simulation-crosshair" />}
      <text x={left} y={263}>{when(new Date(start).toISOString())}</text><text x={right} y={263} textAnchor="end">{end !== start ? when(new Date(end).toISOString()) : ""}</text>
    </svg>}
    <div className="experiments-legend">{series.map((item) => <div key={item.id}><span className="experiments-swatch" style={{ background: color(item.id) }} /><span>{item.name}</span><strong className="num">{eur(item.data.find((entry) => entry.timestamp === selectedTime)?.value?.toString())}</strong></div>)}</div>
    <div className="simulation-curve-caption"><span className="card-note">Línea discontinua: mantener {eur(initial)} en efectivo. Los huecos indican datos sin valoración fiable.</span>{selectedTime != null && <span className="card-note">{when(new Date(selectedTime).toISOString())}</span>}</div>
  </div>;
}

function TradeState({ trade }: { trade: SimulationTrade }) {
  return <><span className={`pill ${trade.status === "waiting_data" ? "warn" : trade.status === "open" ? "ok" : ""}`}>{trade.status === "waiting_data" ? "Esperando datos" : trade.status === "open" ? "Abierta" : "Cerrada"}</span>{trade.had_data_gap && <span className="cell-sub">Con interrupción de datos</span>}</>;
}

export function ExperimentsPanel() {
  const [trial, setTrial] = useState<ExperimentTrial>("costs");
  const query = useQuery({ queryKey: ["experiments", trial, "status"], queryFn: () => getExperimentsStatus(trial), refetchInterval: 5_000, retry: false });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 10_000); return () => window.clearInterval(timer); }, []);
  const data = query.data;
  const strategies = data?.strategies ?? [];
  const selected = strategies.find((strategy) => strategy.id === selectedId) ?? strategies[0];
  const stale = !!data?.running && !!data.last_cycle_at && now - Date.parse(data.last_cycle_at) > 120_000;
  const state = STATES[query.isError ? "error" : stale ? "waiting_data" : data?.state ?? "starting"];
  const initial = data?.reference?.initial_eur ?? "50";
  const positions = Object.values(selected?.positions ?? {});
  const history = [...(selected?.trades ?? [])].sort((a, b) => (b.closed_at ?? b.opened_at).localeCompare(a.closed_at ?? a.opened_at));
  const decisions = [...(selected?.decisions ?? [])].sort((a, b) => b.at.localeCompare(a.at));
  const unreadable = query.isError || stale;
  const costsTrial = trial === "costs";
  const timeframesTrial = trial === "timeframes";
  const labTrial = trial === "lab" || trial === "learned";
  const learnedTrial = trial === "learned";
  const exportPath = `${experimentApiPath(trial)}/export.csv`;
  const selectTrial = (next: ExperimentTrial) => { setTrial(next); setSelectedId(null); };

  return <main className="simulation-panel experiments-panel">
    <header className="topbar simulation-topbar"><div className="brand"><span className="brand-mark" aria-hidden="true">↗</span>invert.io</div><div className="topbar-status"><span className="pill brand">ESTRATEGIAS</span><span className={`pill ${state.tone}`}><span className="dot" />{state.label}</span><span className="pill">Revolut X · EUR · {TRIALS.find((item) => item.id === trial)?.timeframes}</span></div><span className="simulation-update card-note">Último ciclo: {ago(data?.last_cycle_at, now)}</span></header>
    <div className="tabs experiments-trial-tabs" role="group" aria-label="Sesión de comparación">{TRIALS.map((item) => <button key={item.id} className={`tab ${trial === item.id ? "active" : ""}`} aria-pressed={trial === item.id} onClick={() => selectTrial(item.id)}>{item.label}</button>)}</div>
    <div className="simulation-intro"><div><p className="simulation-eyebrow">{costsTrial ? data?.name ?? "Comisiones y frecuencia" : timeframesTrial ? data?.name ?? "Velas de 10 minutos y 1 hora" : labTrial ? data?.name ?? "Estrategias del laboratorio" : "Comparación original"}</p><h1>{costsTrial ? "Cuatro variantes" : timeframesTrial ? "Ocho estrategias" : labTrial ? (strategies.length === 1 ? "Una estrategia" : `${strategies.length || "Las"} estrategias`) : "Cuatro estrategias"}, {eur(initial)} cada una</h1><p className="muted">{learnedTrial ? "Lo que hemos aprendido, puesto a prueba: la ruptura dinámica de 4 horas (la única estrategia nueva que aprobó el laboratorio) y, para cada estrategia, dos cuentas que empiezan a la vez: una compra con orden pasiva (maker, 0 %) y la otra de inmediato (0,09 %). En velas de 1 y 5 minutos la pasiva salió peor; aquí se mide con velas largas." : labTrial ? "Las estrategias que aprobaron el laboratorio (entrenamiento y un año de test fuera de muestra), con sus mismos parámetros y velas (4 horas o 1 hora), ahora con precios reales. Es la prueba que decide si lo del laboratorio se sostiene." : costsTrial ? "La misma señal RSI, con compras inmediatas o maker, y velas de 1 o 5 minutos. Cuatro carteras nuevas con inicio común; la comparación original conserva su historial." : timeframesTrial ? "Las cuatro estrategias de la comparación original, con las mismas reglas, sobre velas de 10 minutos y de 1 hora. Ocho carteras nuevas con inicio común; las de 1 minuto no cambian." : "Cuatro carteras independientes, con el mismo inicio y los mismos mercados. Esta sesión conserva sus saldos e historial originales."}</p></div><span className="pill brand">No usa tu saldo real</span></div>
    <p className="simulation-explanation">{labTrial ? "Como en el laboratorio: compra con orden límite pasiva (0 % de comisión) que caduca a las dos velas si nadie la cruza, venta inmediata al 0,09 %, stop por ATR y salida por la señal de la estrategia, sin objetivo ni caducidad. Tres cuentas solo compran con BTC por encima de su media de 50 o 200 días. Opera poco: harán falta semanas para tener una muestra. Los resultados no garantizan rentabilidad." : timeframesTrial ? "Menos operaciones y movimientos más amplios: el stop y el objetivo se miden con el ATR de la vela larga, así que las comisiones pesan menos en cada operación. Las señales se deciden al cerrar cada vela de 10 minutos o de 1 hora; stop, objetivo y caducidad se revisan cada minuto con el libro de órdenes. Los resultados incluyen costes estimados y no garantizan rentabilidad." : costsTrial ? "Probamos el efecto de las comisiones y de operar con menor frecuencia. Entrada maker: 0 %; ventas: taker al 0,09 %. Las variantes de compra inmediata pagan 0,09 % al entrar y al salir. Usar velas de 5 minutos no garantiza mejorar el resultado." : "Sesión prospectiva: todas empiezan juntas y deciden con datos disponibles en cada ciclo. Las reglas permanecen fijas; no se simulan compras pasadas. Los resultados incluyen costes estimados y no garantizan rentabilidad."}</p>
    {costsTrial && <p className="card-note experiments-maker-note">Una compra maker solo se considera ejecutada si un libro observado después cruza estrictamente su precio límite y ofrece cantidad para toda la orden. Es una aproximación conservadora: no conocemos su puesto en la cola ni podemos asegurar una ejecución real. Una orden puede quedarse sin ejecutar.</p>}
    {labTrial && data?.history_ready && <p className="card-note" role="status">Velas al día: {data.history_ready["4h"] ?? 0} mercados con velas de 4 h y {data.history_ready["1h"] ?? 0} con velas de 1 h, de {data.market_count ?? 0}. Las estrategias necesitan entre 100 y 200 velas seguidas para empezar a decidir.{data.data_errors ? ` ${data.data_errors} series con error de descarga; se reintentan cada minuto.` : ""}</p>}
    {timeframesTrial && data?.history_ready && <p className="card-note" role="status">Velas largas al día: {data.history_ready["5m"] ?? 0} mercados con velas de 10 min y {data.history_ready["1h"] ?? 0} con velas de 1 h, de {data.market_count ?? 0}. Cada estrategia necesita entre 16 y 201 velas seguidas para empezar a decidir.{data.data_errors ? ` ${data.data_errors} series con error de descarga; se reintentan cada minuto.` : ""}</p>}
    {query.isPending && <p className="empty" role="status">Preparando la comparación…</p>}
    {query.isError && <div className="banner bad" role="alert"><span>No se pudo actualizar la comparación: {query.error.message}. Los datos anteriores, si aparecen, son históricos.</span><button className="btn" onClick={() => void query.refetch()}>Reintentar</button></div>}
    {data?.last_error && <p className="banner bad" role="alert">{data.last_error}</p>}
    {data && !data.available && <p className="banner" role="status">La comparación aún no está activada. Cuando se inicie, cada estrategia tendrá su propia cuenta de 50 € ficticios.</p>}
    {data?.available && !data.running && <p className="banner" role="status">La comparación está detenida. Se conserva el historial de la sesión.</p>}
    {stale && <p className="banner" role="status">Han pasado más de dos minutos sin completar un ciclo. Los importes visibles corresponden a la última observación y la clasificación queda pendiente de datos actuales.</p>}
    <section className="card" aria-labelledby="experiments-ranking-title"><div className="card-head"><h2 id="experiments-ranking-title">Rendimiento observado</h2><span className="card-note">Inicio común: {when(data?.started_at)} · {data?.cycles ?? 0} ciclos · {data?.market_count ?? 0} mercados</span></div><p className="card-note">Orden por rendimiento neto, incluyendo posiciones abiertas valorables. Una cuenta sin valoración fiable no recibe puesto. La muestra de operaciones cerradas permite poner el resultado en contexto.</p>
      {!strategies.length ? <p className="empty">Aún no hay estrategias que comparar.</p> : <div className="table-wrap"><table className="experiments-ranking"><thead><tr><th>Puesto / estrategia</th><th className="r">Capital ficticio</th><th className="r">Resultado neto</th><th className="r">Rendimiento</th><th className="r">Comisiones</th><th className="r">Caída máxima</th><th className="r">Cerradas</th><th className="r">Abiertas</th></tr></thead><tbody>{strategies.map((strategy) => <tr key={strategy.id} className={selected?.id === strategy.id ? "experiments-selected" : ""}><td><button className="experiments-strategy-button" onClick={() => setSelectedId(strategy.id)} aria-pressed={selected?.id === strategy.id}><span className="experiments-rank">{unreadable ? "—" : strategy.rank ?? "—"}</span><span className="experiments-swatch" style={{ background: color(strategy.id) }} /><strong>{strategy.name}</strong></button><span className="cell-sub">{strategy.state === "waiting_data" ? "Esperando datos fiables" : strategy.stats.closed_trades === 0 ? "Todavía sin operaciones cerradas" : `${strategy.stats.wins} positivas · ${strategy.stats.losses} negativas`}</span></td><td className="r num">{eur(strategy.stats.equity_eur)}<span className="cell-sub">Libre: {eur(strategy.stats.cash_eur)}</span>{strategy.execution === "maker_entry" && <span className="cell-sub">Reservado: {eur(strategy.stats.reserved_eur ?? "0")}</span>}</td><td className={`r num ${tone(strategy.stats.net_pnl_eur)}`}>{eur(strategy.stats.net_pnl_eur, true)}</td><td className={`r num ${tone(strategy.stats.return_pct)}`}>{rate(strategy.stats.return_pct)}</td><td className="r num">{eur(strategy.stats.fees_eur)}</td><td className="r num">{rate(strategy.stats.max_drawdown_pct, false)}</td><td className="r num">{strategy.stats.closed_trades}</td><td className="r num">{strategy.stats.open_positions}<span className="cell-sub">{strategy.stats.waiting_positions} esperando datos</span>{strategy.execution === "maker_entry" && <span className="cell-sub">{strategy.stats.pending_orders ?? Object.keys(strategy.pending_orders ?? {}).length} órdenes pendientes</span>}</td></tr>)}<tr className="experiments-reference"><td><strong>{data?.reference?.name ?? "Mantener efectivo"}</strong><span className="cell-sub">Referencia sin operar</span></td><td className="r num">{eur(data?.reference?.equity_eur ?? initial)}</td><td className="r num">{eur("0")}</td><td className="r num">{rate(data?.reference?.return_pct ?? "0")}</td><td className="r num">{eur("0")}</td><td className="r num">{rate("0", false)}</td><td className="r num">0</td><td className="r num">0</td></tr></tbody></table></div>}
      <p className="card-note experiments-table-note">Caída máxima: retroceso desde el mayor capital observado. Comisiones pagadas, spread y deslizamiento están incorporados al resultado; no hay que restarlos otra vez.</p>
    </section>
    <section className="card section" aria-labelledby="experiments-closed-costs-title"><div className="card-head"><h2 id="experiments-closed-costs-title">Qué parte del resultado son comisiones</h2><span className="card-note">Solo operaciones cerradas</span></div><p className="card-note">El resultado antes de comisiones todavía incluye los precios del libro: spread y deslizamiento. Esta separación permite ver si quitar las comisiones bastaría, sin inventar otras ejecuciones.</p>
      {!strategies.length ? <p className="empty">Esperando resultados de las carteras.</p> : <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Estrategia</th><th className="r">Cerradas</th><th className="r">Antes de comisiones</th><th className="r">Comisiones</th><th className="r">Resultado neto</th></tr></thead><tbody>{strategies.map((strategy) => <tr key={strategy.id}><td><span className="experiments-swatch" style={{ background: color(strategy.id) }} /> {strategy.name}</td><td className="r num">{strategy.stats.closed_trades}</td><td className={`r num ${tone(strategy.stats.closed_gross_eur)}`}>{eur(strategy.stats.closed_gross_eur, true)}</td><td className="r num">{eur(strategy.stats.closed_fees_eur)}</td><td className={`r num ${tone(strategy.stats.realized_pnl_eur)}`}>{eur(strategy.stats.realized_pnl_eur, true)}</td></tr>)}</tbody></table></div>}
    </section>
    <section className="card section" aria-labelledby="experiments-curve-title"><div className="card-head"><h2 id="experiments-curve-title">Evolución de las carteras</h2><a className="btn simulation-export" href={exportPath} download>Exportar comparación CSV</a></div><ComparisonCurve key={trial} strategies={strategies} initial={initial} /></section>
    {selected && <>
      <div className="experiments-detail-head section"><div><p className="simulation-eyebrow">Detalle de estrategia</p><h2>{selected.name}</h2></div><label>Consultar estrategia<select className="select" aria-label="Consultar estrategia" value={selected.id} onChange={(event) => setSelectedId(event.target.value)}>{strategies.map((strategy) => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}</select></label></div>
      <div className="grid grid-half section"><section className="card" aria-labelledby="experiments-rules-title"><div className="card-head"><h2 id="experiments-rules-title">Reglas de {selected.name}</h2></div><p>{selected.description}</p><ul className="experiments-rules-list">{selected.rules.map((rule, index) => <li key={index}>{rule}</li>)}</ul><dl className="simulation-rules"><div><dt>Capital / entrada</dt><dd>{eur(selected.config.initial_eur)} / {eur(selected.config.allocation_eur)}</dd></div><div><dt>Posiciones / caducidad</dt><dd>{selected.config.max_positions} / {selected.config.lifetime_minutes >= 525600 ? "sin caducidad" : duration(selected.config.lifetime_minutes)}</dd></div><div><dt>Stop / objetivo</dt><dd>{selected.config.stop_atr} ATR / {Number(selected.config.target_atr) >= 1000 ? "sin objetivo" : `${selected.config.target_atr} ATR`}</dd></div><div><dt>Comisión entrada / salida</dt><dd>{selected.execution === "maker_entry" ? "0 %" : rate(selected.config.fee_pct, false)} / {rate(selected.config.fee_pct, false)}</dd></div>{selected.timeframe_minutes != null && <div><dt>Velas / pausa tras salir</dt><dd>{duration(selected.timeframe_minutes)} / {selected.cooldown_minutes ?? 0} min</dd></div>}{selected.execution === "maker_entry" && <div><dt>Caducidad de orden pendiente</dt><dd>{selected.pending_minutes == null ? "—" : duration(selected.pending_minutes)}</dd></div>}{selected.lab_result && <div><dt>En el test del laboratorio (un año)</dt><dd>{rate(String(selected.lab_result.median_return_pct))} de mediana por moneda · {rate(String(selected.lab_result.positive_pct), false)} de monedas en positivo · {selected.lab_result.trades_per_coin.toLocaleString("es-ES")} operaciones por moneda · comprar y mantener {rate(String(selected.lab_result.buy_and_hold_pct))}</dd></div>}<div><dt>Positivas sobre cerradas</dt><dd>{rate(selected.stats.win_rate_pct, false)}</dd></div><div><dt>Ganancias / pérdidas</dt><dd>{number(selected.stats.profit_factor) == null ? "—" : qty(number(selected.stats.profit_factor)!)}</dd></div></dl><p className="card-note">{selected.execution === "maker_entry" ? "Entrada maker pendiente al límite fijado; la salida se simula como taker al libro observado." : "Entrada y salida al libro observado."} Stop y objetivo no garantizan precio. «Ganancias / pérdidas» queda sin valor cuando no se puede calcular.</p>{selected.model_limitations && <p className="card-note">{selected.model_limitations}</p>}{selected.sources.length > 0 && <div className="experiments-sources"><span className="card-note">Referencias de las reglas:</span>{selected.sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>)}</div>}</section>
      <section className="card" aria-labelledby="experiments-decisions-title"><div className="card-head"><h2 id="experiments-decisions-title">Decisiones de {selected.name}</h2><span className="card-note">Últimas {decisions.length}</span></div>{!decisions.length ? <p className="empty">Aún no hay decisiones. Esperando las condiciones de esta estrategia.</p> : <div className="simulation-decisions">{decisions.map((decision, index) => <article key={`${decision.at}-${decision.opportunity_id}-${index}`}><div><strong>{decision.symbol}</strong><span className={`pill ${decision.action === "skip" || decision.action === "wait" ? "warn" : ""}`}>{ACTIONS[decision.action]}</span><time className="card-note" dateTime={decision.at}>{when(decision.at)}</time></div><p>{decision.reason}</p></article>)}</div>}</section></div>
      {selected.execution === "maker_entry" && <MakerOrders strategy={selected} exportUrl={`${experimentApiPath(trial)}/maker-orders.csv?strategy_id=${encodeURIComponent(selected.id)}`} />}
      <section className="card section" aria-labelledby="experiments-positions-title"><div className="card-head"><h2 id="experiments-positions-title">Posiciones de {selected.name}</h2><span className="card-note">Niveles originales de cada entrada</span></div>{!positions.length ? <p className="empty">Sin posiciones abiertas. Esta cartera espera señales con saldo y cupo disponibles.</p> : <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Mercado / entrada</th><th>Cantidad</th><th>Precio simulado</th><th>Stop / objetivo</th><th>Estado</th></tr></thead><tbody>{positions.map((trade) => <tr key={trade.id}><td><strong>{trade.symbol}</strong><span className="cell-sub">{when(trade.opened_at)}</span></td><td className="num">{qty(Number(trade.quantity))}</td><td className="num">{price(number(trade.entry))} €</td><td className="num">{price(number(trade.stop))} €<span className="cell-sub">{price(number(trade.target))} €</span></td><td><TradeState trade={trade} /></td></tr>)}</tbody></table></div>}</section>
      <section className="card section" aria-labelledby="experiments-history-title"><div className="card-head"><h2 id="experiments-history-title">Operaciones cerradas de {selected.name}</h2><a className="btn simulation-export" href={`${exportPath}?strategy_id=${encodeURIComponent(selected.id)}`} download>Exportar estrategia CSV</a></div><p className="card-note">Historial ficticio de esta sesión. El CSV incluye el historial completo disponible.</p>{!history.length ? <p className="empty">Todavía no hay operaciones cerradas en esta estrategia.</p> : <div className="table-wrap"><table className="simulation-table"><thead><tr><th>Mercado / fechas</th><th>Entrada / salida</th><th>Comisiones entrada / salida</th><th className="r">Resultado neto</th><th>Motivo</th></tr></thead><tbody>{history.map((trade) => <tr key={trade.id}><td><strong>{trade.symbol}</strong><span className="cell-sub">Entrada: {when(trade.opened_at)}</span><span className="cell-sub">Salida: {when(trade.closed_at)}</span>{trade.had_data_gap && <span className="cell-sub">Con interrupción de datos</span>}</td><td className="num">{price(number(trade.entry))} €<span className="cell-sub">{trade.exit_price == null ? "—" : `${price(number(trade.exit_price))} €`}</span></td><td className="num">{eur(trade.entry_fee_eur)}<span className="cell-sub">{eur(trade.exit_fee_eur)}</span></td><td className={`r num ${tone(trade.pnl_eur)}`}>{eur(trade.pnl_eur, true)}</td><td className="simulation-reason">{trade.reason ?? "—"}</td></tr>)}</tbody></table></div>}</section>
    </>}
  </main>;
}
