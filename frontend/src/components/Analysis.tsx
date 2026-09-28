import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  get,
  type AnalysisCosts,
  type AnalysisDetail,
  type AnalysisObservation,
  type AnalysisOpportunity,
  type AnalysisRanking,
  type AnalysisStatus,
  type AnalysisSummary,
} from "../api";

const STATE: Record<string, string> = {
  starting: "Preparando datos", running: "Analizando", stopped: "Análisis parado", degraded: "Datos incompletos",
  warming: "Cargando histórico", watching: "En observación", eligible: "Condiciones cumplidas", open: "Seguimiento abierto",
  unavailable: "Sin datos válidos", closed: "Finalizada", interrupted: "Interrumpida", complete: "Completa",
  incomplete: "Incompleta", ambiguous: "Ambigua", pending: "Pendiente", sent: "Enviado", failed: "Error de envío", expired: "Caducada",
  target: "Objetivo alcanzado", stop: "Stop alcanzado", expired_opportunity: "Caducada", score: "Señal debilitada",
  filters: "Filtros incumplidos", data_loss: "Pérdida de datos", take_profit: "Objetivo alcanzado", stop_loss: "Stop alcanzado",
};
const FACTORS: Record<string, string> = {
  trend: "Tendencia", momentum: "Momentum", macd: "MACD", rsi: "RSI", volume: "Volumen", breakout: "Ruptura",
  tendencia: "Tendencia", volumen: "Volumen", ruptura: "Ruptura",
};
const label = (value: string | null | undefined) => value ? STATE[value] ?? value.replaceAll("_", " ") : "—";
const symbol = (market: string) => market.split(":").at(-1) ?? market;
const numeric = (value: number | null | undefined, digits = 2) =>
  value == null || !Number.isFinite(value) ? "—" : value.toLocaleString("es-ES", { maximumFractionDigits: digits, minimumFractionDigits: digits });
const scoreText = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString("es-ES", { maximumFractionDigits: 2 });
const percent = (value: number | null | undefined) => value == null ? "—" : `${numeric(value)} %`;
const euro = (value: number | null | undefined) => value == null ? "—" : `${value.toLocaleString("es-ES", { maximumFractionDigits: value < 1 ? 8 : 4 })} €`;
const timestamp = (value: string | null | undefined) => value ? new Date(value).toLocaleString("es-ES") : "—";
const age = (value: string | null | undefined) => {
  if (!value) return "sin datos";
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds)) return "sin datos";
  if (seconds < 60) return `hace ${seconds} s`;
  if (seconds < 3600) return `hace ${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `hace ${Math.floor(seconds / 3600)} h`;
  return `hace ${Math.floor(seconds / 86400)} d`;
};
const qualityTone = (quality: string) => quality === "complete" ? "ok" : "warn";

function Reasons({ positive, negative }: { positive: string[]; negative: string[] }) {
  return <div className="analysis-reasons">
    <div><h4>A favor</h4>{positive.length ? <ul>{positive.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="muted">Sin factores favorables registrados.</p>}</div>
    <div><h4>En contra</h4>{negative.length ? <ul>{negative.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="muted">Sin factores negativos registrados.</p>}</div>
  </div>;
}

function CostDetails({ costs }: { costs: AnalysisCosts | null }) {
  if (!costs) return <p className="muted">Profundidad pendiente: aún no hay estimación de ejecución.</p>;
  return <div className="analysis-costs">
    <dl className="analysis-metrics">
      <div><dt>Entrada estimada</dt><dd>{euro(costs.entry)}</dd></div>
      <div><dt>Stop</dt><dd>{euro(costs.stop)}</dd></div>
      <div><dt>Objetivo</dt><dd>{euro(costs.target)}</dd></div>
      <div><dt>Coste ida y vuelta</dt><dd>{percent(costs.round_trip_cost_pct)} <small>({euro(costs.round_trip_cost_eur)})</small></dd></div>
      <div><dt>Deslizamiento compra / venta</dt><dd>{percent(costs.buy_slippage_pct)} / {percent(costs.sell_slippage_pct)}</dd></div>
      <div><dt>Objetivo neto estimado</dt><dd>{percent(costs.target_net_pct)}</dd></div>
    </dl>
    {!!costs.reasons.length && <p className="analysis-warning">{costs.reasons.join(" · ")}</p>}
    <p className="card-note">Comisiones, spread y deslizamiento estimados incluidos. Los precios de ejecución no están garantizados.</p>
  </div>;
}

function Observation({ row, onSelect }: { row: AnalysisObservation; onSelect: (id: string) => void }) {
  return <details className={`analysis-row ${row.state === "open" ? "analysis-row-open" : ""}`}>
    <summary>
      <span className="analysis-market"><strong>{symbol(row.market)}</strong><span className="cell-sub">Vol. 24 h {euro(row.quote_volume)}</span></span>
      <span className="analysis-score"><strong className={row.score != null && row.score >= 70 ? "up" : ""}>{scoreText(row.score)}</strong><span className="cell-sub">puntos</span></span>
      <span className="analysis-row-price num">{euro(row.price)}<span className="cell-sub">Spread {percent(row.spread_pct)}</span></span>
      <span className={`pill ${row.state === "open" || row.state === "eligible" ? "ok" : row.state === "unavailable" ? "warn" : ""}`}>{label(row.state)}</span>
      <span className="analysis-freshness cell-sub" title={`Cotización ${timestamp(row.quote_ts)} · Observación ${timestamp(row.observed_at)}`}>Cotización {age(row.quote_ts)}<span>Vela {age(row.bar_ts)}</span></span>
      <span className="analysis-expand" aria-hidden="true">⌄</span>
    </summary>
    <div className="analysis-row-body">
      <div className="analysis-factor-list">{Object.entries(row.points).map(([factor, points]) => <span className="pill" key={factor}>{FACTORS[factor] ?? factor}: {scoreText(points)}{typeof row.max_points === "object" && row.max_points[factor] != null ? `/${row.max_points[factor]}` : ""}</span>)}</div>
      <Reasons positive={row.positive} negative={row.negative} />
      {!!row.reasons.length && <p className="analysis-warning">{row.reasons.join(" · ")}</p>}
      <CostDetails costs={row.costs} />
      <div className="analysis-row-footer"><span className="card-note">ATR {percent(row.atr_pct)} · Observación {timestamp(row.observed_at)} · Vela {timestamp(row.bar_ts)}</span>
        {row.opportunity_id && <button className="btn" onClick={() => onSelect(row.opportunity_id!)}>Ver seguimiento</button>}
      </div>
    </div>
  </details>;
}

function OpportunityDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const query = useQuery({
    queryKey: ["analysis", "detail", id],
    queryFn: () => get<AnalysisDetail>(`/api/analysis/opportunities/${encodeURIComponent(id)}`),
    refetchInterval: 30_000,
  });
  const data = query.data;
  return <section className="card section analysis-detail" aria-labelledby="analysis-detail-title" id="analysis-detail">
    <div className="card-head"><h2 id="analysis-detail-title">{data ? `Seguimiento · ${symbol(data.market)}` : "Detalle del seguimiento"}</h2><button className="btn" onClick={onClose}>Cerrar detalle</button></div>
    {query.isPending && <p className="empty" role="status">Cargando seguimiento…</p>}
    {query.isError && <p className="analysis-warning" role="alert">No se pudo cargar el seguimiento. {query.error.message}</p>}
    {data && <>
      <div className="analysis-detail-intro"><span className={`pill ${data.status === "open" ? "ok" : ""}`}>{label(data.status)}</span><span className={`pill ${qualityTone(data.quality)}`}>{label(data.quality)}</span><span className="muted">{timestamp(data.created_at)}{data.ended_at ? ` → ${timestamp(data.ended_at)}` : ""}</span></div>
      <p>{data.reason ?? (data.outcome ? label(data.outcome) : "Esperando el desenlace de la señal.")}</p>
      {data.quality !== "complete" && <p className="analysis-warning">{data.quality === "ambiguous" ? "No se conoce el orden de los eventos. Esta oportunidad no tiene un resultado atribuible." : "Faltan datos para reconstruir el seguimiento completo. No se atribuye ganancia ni pérdida a los tramos desconocidos."}</p>}
      <dl className="analysis-metrics">
        <div><dt>Entrada original</dt><dd>{euro(data.entry)}</dd></div><div><dt>Stop original</dt><dd>{euro(data.stop)}</dd></div><div><dt>Objetivo original</dt><dd>{euro(data.target)}</dd></div>
        <div><dt>Importe de referencia</dt><dd>{euro(data.reference_eur)}</dd></div><div><dt>Puntuación inicial</dt><dd>{scoreText(data.score)} puntos</dd></div><div><dt>Resultado neto hipotético</dt><dd>{percent(data.estimated_return_pct)}</dd></div>
        <div><dt>Máximo avance favorable</dt><dd className="up">{percent(data.mfe_pct)}</dd></div><div><dt>Máximo movimiento adverso</dt><dd className="down">{percent(data.mae_pct)}</dd></div><div><dt>Última actualización</dt><dd className="analysis-small-value">{timestamp(data.updated_at)}</dd></div>
      </dl>
      <Reasons positive={data.positive} negative={data.negative} />
      <h3 className="analysis-subtitle">Evolución desde la entrada</h3>
      <div className="analysis-horizons">{(["15m", "1h", "4h"] as const).map((horizon) => {
        const item = data.horizons[horizon];
        return <div key={horizon}><strong>{horizon === "15m" ? "15 minutos" : horizon === "1h" ? "1 hora" : "4 horas"}</strong><span className="analysis-horizon-value">{percent(item?.net_return_pct)}</span><span className="cell-sub">Neto estimado · Bruto {percent(item?.return_pct)}</span><span className={`pill ${item && item.at ? qualityTone(item.quality) : ""}`}>{item ? label(item.quality) : "Pendiente"}</span>{item?.at && <span className="cell-sub">{timestamp(item.at)}</span>}</div>;
      })}</div>
      <p className="card-note">Evolución y extremos observados hasta cuatro horas desde la entrada, incluso después del cierre de la señal. Son resultados hipotéticos, no operaciones ejecutadas. Los horizontes incompletos se muestran sin rendimiento.</p>
      <details className="analysis-disclosure"><summary>Costes y factores originales</summary><CostDetails costs={data.costs} /><p className="card-note">Reglas {data.rule_version} · Cantidad {numeric(data.quantity, 8)} · Vela inicial {timestamp(data.bar_ts)}</p><div className="analysis-factor-list">{Object.entries(data.points).map(([key, points]) => <span className="pill" key={key}>{FACTORS[key] ?? key}: {scoreText(points)}</span>)}</div><details><summary>Configuración guardada</summary><pre className="analysis-config">{JSON.stringify(data.config, null, 2)}</pre></details></details>
      <details className="analysis-disclosure"><summary>Avisos de Telegram ({data.notifications.length})</summary>
        {!data.notifications.length ? <p className="muted">No hay avisos registrados.</p> : <ul className="analysis-notifications">{data.notifications.map((item, index) => <li key={item.id ?? index}><strong>{item.kind === "start" ? "Inicio" : item.kind === "end" ? "Fin" : label(item.kind)}</strong><span className={`pill ${item.status === "sent" ? "ok" : item.status === "failed" ? "bad" : ""}`}>{label(item.status)}</span><span className="cell-sub">{timestamp(item.sent_at ?? item.created_at)}</span>{item.last_error && <span className="down">{item.last_error}</span>}</li>)}</ul>}
      </details>
      <details className="analysis-disclosure"><summary>Observaciones guardadas ({data.observations.length})</summary><div className="table-wrap analysis-observations"><table><thead><tr><th>Fecha</th><th>Vela</th><th>Puntos</th><th>Precio</th><th>Estado</th></tr></thead><tbody>{data.observations.map((row, index) => <tr key={`${row.observed_at}-${index}`}><td>{timestamp(row.observed_at)}</td><td>{timestamp(row.bar_ts)}</td><td>{scoreText(row.score)}</td><td>{euro(row.price)}</td><td>{label(row.state)}</td></tr>)}</tbody></table></div></details>
    </>}
  </section>;
}

const PERIODS = [{ value: "24h", label: "24 horas", hours: 24 }, { value: "7d", label: "7 días", hours: 168 }, { value: "30d", label: "30 días", hours: 720 }, { value: "all", label: "Todo", hours: 0 }];

export function AnalysisDashboard({ status, statusError, connected }: { status: AnalysisStatus | undefined; statusError: Error | null; connected: boolean }) {
  const client = useQueryClient();
  const [period, setPeriod] = useState("24h");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [marketSearch, setMarketSearch] = useState("");
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, []);
  const periodAnchor = Math.floor(now / 60_000);
  const params = useMemo(() => {
    const hours = PERIODS.find((item) => item.value === period)?.hours ?? 0;
    return hours ? `?since=${encodeURIComponent(new Date(periodAnchor * 60_000 - hours * 3600_000).toISOString())}` : "";
  }, [period, periodAnchor]);
  const ranking = useQuery({ queryKey: ["analysis", "ranking"], queryFn: () => get<AnalysisRanking>("/api/analysis/ranking"), refetchInterval: 30_000 });
  const opportunities = useQuery({ queryKey: ["analysis", "opportunities", params], queryFn: () => get<AnalysisOpportunity[]>(`/api/analysis/opportunities${params}`), refetchInterval: 30_000 });
  const summary = useQuery({ queryKey: ["analysis", "summary", period === "all" ? "all" : params], queryFn: () => get<AnalysisSummary>(`/api/analysis/summary${params}`), refetchInterval: 30_000 });
  const stats = summary.data;
  const versions = Object.entries(stats?.by_version ?? {});
  const rows = ranking.data?.rows ?? [];
  const allMarkets = status?.coverage?.scope === "all_eur" || status?.config.market_scope === "all_eur";
  const coverage = status?.coverage;
  const observed = coverage?.observed ?? status?.selected.length ?? 0;
  const catalog = allMarkets ? coverage?.catalog_eur : Number(status?.config.max_markets ?? 20);
  const search = marketSearch.trim().toLocaleUpperCase("es-ES");
  const filteredRows = [...rows].filter((row) => symbol(row.market).toLocaleUpperCase("es-ES").includes(search))
    .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  const excluded = (ranking.data?.excluded ?? []).filter((row) => !allMarkets || symbol(row.market).split("/")[1] === "EUR");
  const select = (id: string) => { setSelectedId(id); window.setTimeout(() => document.getElementById("analysis-detail")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0); };

  return <main className="analysis-dashboard">
    <header className="topbar analysis-topbar">
      <div className="brand"><span className="brand-mark" aria-hidden="true">↗</span>invert.io</div>
      <div className="topbar-status"><span className="pill brand">ANÁLISIS</span><span className={`pill ${status?.running && status.state === "running" ? "ok" : "warn"}`}><span className="dot" />{status ? label(status.state) : "Sin conexión"}</span><span className="pill">Revolut X · EUR · EEA</span></div>
      <span className="analysis-live cell-sub"><span className={`pill ${connected ? "ok" : ""}`}>{connected ? "En vivo" : "Reconectando"}</span> Cada {status?.interval_seconds ?? 60} s · Velas {status?.timeframe ?? "1m"}</span>
    </header>

    <div className="analysis-intro"><div><p className="analysis-eyebrow">Observatorio intradía</p><h1>Señales con contexto</h1><p className="muted">Puntuaciones experimentales basadas en reglas. Tú decides y ejecutas las compras.</p></div><span className="pill">Sin órdenes automáticas</span></div>
    {statusError && <div className="banner bad" role="alert"><span>No se pudo consultar el análisis: {statusError.message}. Arranca <code>uv run invertio analyze</code>.</span><button className="btn" onClick={() => void client.invalidateQueries({ queryKey: ["analysis"] })}>Reintentar</button></div>}
    {status && !status.running && <div className="banner" role="status"><span>El análisis está parado. Puedes consultar el historial. Para recopilar datos ejecuta <code>uv run invertio analyze</code>.</span></div>}
    {status?.last_error && <div className="banner bad" role="alert">{status.last_error}</div>}

    <div className="kpis analysis-kpis">
      <section className="card"><div className="kpi-label">{allMarkets ? "Mercados observados" : "Mercados seleccionados"}</div><div className="kpi-value">{observed}{catalog != null && <small> / {catalog}</small>}</div><div className="kpi-sub">{allMarkets ? "Todos los pares EUR activos" : "Ordenados por volumen y liquidez"}</div>{allMarkets && coverage && <div className="kpi-sub">{coverage.entry_eligible} pasan filtros de entrada · {coverage.excluded} excluidos de entrada</div>}</section>
      <section className="card"><div className="kpi-label">Histórico preparado</div><div className="kpi-value">{status?.warmup.ready ?? 0}<small> / {status?.warmup.total ?? 0}</small></div><div className="progress" role="progressbar" aria-label="Mercados con histórico preparado" aria-valuenow={status?.warmup.ready ?? 0} aria-valuemin={0} aria-valuemax={status?.warmup.total || 1}><span style={{ width: `${status?.warmup.total ? 100 * status.warmup.ready / status.warmup.total : 0}%` }} /></div></section>
      <section className="card"><div className="kpi-label">Último ciclo</div><div className="kpi-value analysis-small-kpi">{age(status?.last_cycle_at)}</div><div className="kpi-sub" title={timestamp(status?.last_cycle_at)}>{status?.cycles ?? 0} ciclos completados</div></section>
      <section className="card"><div className="kpi-label">Telegram</div><div className="kpi-value analysis-small-kpi">{status?.telegram.configured ? "Configurado" : "Sin configurar"}</div><div className="kpi-sub">{status?.telegram.configured ? `${status.telegram.sent} enviados · ${status.telegram.pending} pendientes · ${status.telegram.failed} fallidos` : "El panel funciona sin Telegram"}</div></section>
    </div>
    {status && !status.telegram.configured && <p className="analysis-telegram-note">Para recibir avisos configura <code>TELEGRAM_BOT_TOKEN</code> y <code>TELEGRAM_CHAT_ID</code> antes de arrancar el análisis.</p>}
    {allMarkets && <p className="analysis-help">Se observan todos los pares EUR activos, incluidas las monedas que no pasan los filtros. Cada entrada exige además señal, liquidez y costes válidos. Los nuevos mercados se incorporan desde ahora; no se simulan compras retroactivas.</p>}

    <section className="card" aria-labelledby="analysis-ranking-title">
      <div className="card-head"><h2 id="analysis-ranking-title">Ranking actual</h2><span className="card-note">Actualizado {age(ranking.data?.updated_at)}</span></div>
      <p className="analysis-help">La nota expresa fuerza de las reglas, no probabilidad de ganar. Abre una fila para ver motivos, costes y niveles.</p>
      {!!rows.length && <div className="analysis-market-search"><label htmlFor="analysis-market-search">Buscar moneda<input id="analysis-market-search" type="search" value={marketSearch} onChange={(event) => setMarketSearch(event.target.value)} placeholder="BTC, XRP…" /></label><span className="card-note" role="status">{filteredRows.length} de {rows.length} mercados</span></div>}
      {ranking.isPending ? <p className="empty" role="status">Cargando ranking…</p> : ranking.isError ? <p className="analysis-warning" role="alert">No se pudo cargar el ranking. {ranking.error.message}</p> : !rows.length ? <p className="empty">Aún no hay mercados preparados. El primer ciclo incorporará monedas y descargará su histórico.</p> : !filteredRows.length ? <p className="empty">Ningún mercado coincide con la búsqueda.</p> : <div className="analysis-ranking-rows">{filteredRows.map((row) => <Observation key={row.market} row={row} onSelect={select} />)}</div>}
      {!!excluded.length && <details className="analysis-disclosure"><summary>{allMarkets ? "Mercados EUR excluidos de entrada" : "Mercados excluidos"} ({excluded.length})</summary><div className="analysis-excluded">{excluded.map((row) => <div key={row.market}><strong>{symbol(row.market)}</strong><span className="muted">{row.reasons.join(" · ")}</span></div>)}</div></details>}
    </section>

    <section className="card section" aria-labelledby="analysis-history-title">
      <div className="card-head"><h2 id="analysis-history-title">Historial y resultados hipotéticos</h2><div className="analysis-history-actions"><div className="tabs" role="group" aria-label="Periodo del historial">{PERIODS.map((item) => <button className={`tab ${period === item.value ? "active" : ""}`} aria-pressed={period === item.value} key={item.value} onClick={() => setPeriod(item.value)}>{item.label}</button>)}</div><a className="btn" href={`/api/analysis/export.csv${params}`} download>Exportar CSV</a></div></div>
      {summary.isError && <p className="analysis-warning" role="alert">No se pudo cargar el resumen.</p>}
      {stats && <><div className="analysis-summary">
        <div><strong>{stats.total}</strong><span>Oportunidades de la muestra</span></div><div><strong>{stats.open}</strong><span>Abiertas</span></div><div><strong>{stats.complete}</strong><span>Completas</span></div><div><strong>{stats.incomplete}</strong><span>Incompletas</span></div><div><strong>{stats.ambiguous}</strong><span>Ambiguas</span></div><div><strong>{versions.length > 1 ? "Por versión" : percent(stats.mean_return_pct)}</strong><span>Media neta · n={stats.known_results ?? 0}</span></div>
      </div><p className="card-note">{stats.closed} finalizadas · {stats.interrupted} interrumpidas · {stats.positive} resultados positivos. La muestra corresponde a oportunidades iniciadas en el periodo seleccionado.</p>
        {!!versions.length && <details className="analysis-disclosure" open={versions.length > 1}><summary>Resultados por versión de reglas y configuración ({versions.length})</summary><p className="card-note">Cada versión conserva sus criterios originales. Las medias se comparan por separado.</p><div className="table-wrap"><table><thead><tr><th>Versión</th><th>Muestra total</th><th>Resultados conocidos</th><th>Positivos</th><th>Media neta</th></tr></thead><tbody>{versions.map(([version, values]) => <tr key={version}><td><code>{version}</code></td><td>{values.total}</td><td>{values.known_results}</td><td>{values.positive}</td><td>{percent(values.mean_return_pct)}</td></tr>)}</tbody></table></div></details>}
        {!!opportunities.data?.length && <p className="card-note">Mostrando {opportunities.data.length} de {stats.total} oportunidades; el CSV incluye todo el periodo.</p>}
      </>}
      {opportunities.isPending ? <p className="empty" role="status">Cargando historial…</p> : opportunities.isError ? <p className="analysis-warning" role="alert">No se pudo cargar el historial. {opportunities.error.message}</p> : !opportunities.data?.length ? <p className="empty">No hay oportunidades en este periodo. Se registrarán cuando cumplan las reglas y los filtros.</p> : <div className="table-wrap"><table className="analysis-history"><thead><tr><th>Inicio</th><th>Mercado</th><th>Puntos</th><th>Entrada / stop / objetivo</th><th>Desenlace</th><th>Calidad</th><th className="r">Neto hipotético</th></tr></thead><tbody>{opportunities.data.map((item) => <tr key={item.id} className={selectedId === item.id ? "analysis-selected" : ""}><td>{timestamp(item.created_at)}</td><td><button className="link" onClick={() => select(item.id)} aria-label={`Ver seguimiento de ${symbol(item.market)} del ${timestamp(item.created_at)}`}>{symbol(item.market)}</button></td><td className="num">{scoreText(item.score)}</td><td className="num">{euro(item.entry)}<span className="cell-sub">{euro(item.stop)} / {euro(item.target)}</span></td><td>{item.outcome ? label(item.outcome) : label(item.status)}{item.ended_at && <span className="cell-sub">{timestamp(item.ended_at)}</span>}</td><td><span className={`pill ${qualityTone(item.quality)}`}>{label(item.quality)}</span></td><td className={`r num ${item.estimated_return_pct == null ? "" : item.estimated_return_pct >= 0 ? "up" : "down"}`}>{percent(item.estimated_return_pct)}</td></tr>)}</tbody></table></div>}
    </section>
    {selectedId && <OpportunityDetail id={selectedId} onClose={() => setSelectedId(null)} />}
  </main>;
}
