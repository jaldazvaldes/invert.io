import type { MarketStatus, Score, Status, Trade, Backtest } from "../api";
import {
  ago,
  EXIT_REASONS,
  FACTOR_LABELS,
  money,
  num,
  pct,
  points,
  price,
  qty,
  tone,
  when,
} from "../format";

export function VenueCards({ status }: { status: Status }) {
  if (status.venues.length === 0) {
    return <p className="empty">Sin datos de capital todavía.</p>;
  }
  return (
    <div className="kpis">
      {status.venues.map((v) => {
        const open = v.positions.length;
        const unrealized = v.positions.reduce((sum, p) => sum + p.unrealized, 0);
        const todayPnl = status.today?.pnl[v.currency];
        return (
          <section className="card" key={v.venue} aria-label={`Capital en ${v.venue}`}>
            <div className="kpi-label">
              <span>Capital · {v.venue}</span>
              <span className="num">inicial {money(v.initial, v.currency)}</span>
            </div>
            <div className="kpi-value num">{money(v.equity, v.currency)}</div>
            <div className="kpi-sub">
              <span className={`num ${tone(v.return_pct)}`}>{pct(v.return_pct)} desde el inicio</span>
              {todayPnl !== undefined && (
                <span className={`num ${tone(todayPnl)}`}>hoy {money(todayPnl, v.currency, true)}</span>
              )}
            </div>
            <div className="kpi-sub" style={{ marginTop: 6 }}>
              <span className="num">efectivo {money(v.cash, v.currency)}</span>
              <span className="num">
                {open} {open === 1 ? "posición" : "posiciones"}
                {open > 0 && (
                  <span className={tone(unrealized)}> ({money(unrealized, v.currency, true)})</span>
                )}
              </span>
            </div>
          </section>
        );
      })}
    </div>
  );
}

function FactorBars({ score }: { score: Score }) {
  return (
    <div className="factors">
      {score.factors.map((f) => {
        const max = score.max_points[f] || 1;
        const value = score.points[f] ?? 0;
        return (
          <div className="factor" key={f} title={`${FACTOR_LABELS[f]}: ${points(value)} de ${max}`}>
            <span>
              {FACTOR_LABELS[f]} <span className="num">{points(value)}/{max}</span>
            </span>
            <div className="factor-bar">
              <span style={{ width: `${(value / max) * 100}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function ScoresCard({ scores, running }: { scores: Score[] | undefined; running: boolean }) {
  return (
    <section className="card" aria-labelledby="scores-title">
      <div className="card-head">
        <h2 id="scores-title">Puntuación ahora</h2>
        <span className="card-note">entrada con 70 o más</span>
      </div>
      {!running && <p className="empty">Se calcula con el motor en marcha.</p>}
      {running && (!scores || scores.length === 0) && (
        <p className="empty">
          Ningún mercado vigilado usa la estrategia de puntuación (config/live.yaml).
        </p>
      )}
      {scores?.map((s) => (
        <div className="score-row" key={s.market}>
          <div className="score-top">
            <div>
              <strong>{s.symbol}</strong>{" "}
              <span className="cell-sub">
                {price(s.close)} · ATR {pct(s.atr_pct, false)}
              </span>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {s.meets && <span className="pill ok">cumple</span>}
              {!s.tradable && <span className="pill warn">no cubre costes</span>}
              <span className="score-value num">{points(s.score)}</span>
            </div>
          </div>
          <div className="score-meter" aria-hidden="true">
            <span style={{ width: `${s.score}%` }} />
          </div>
          <FactorBars score={s} />
        </div>
      ))}
    </section>
  );
}

interface MarketsProps {
  markets: MarketStatus[];
  running: boolean;
  onToggle: (market: string, enabled: boolean) => void;
}

export function MarketsCard({ markets, running, onToggle }: MarketsProps) {
  return (
    <section className="card" aria-labelledby="markets-title">
      <div className="card-head">
        <h2 id="markets-title">Mercados vigilados</h2>
        <span className="card-note">desactivar = no abre posiciones nuevas</span>
      </div>
      {markets.length === 0 && <p className="empty">No hay mercados en config/live.yaml.</p>}
      {markets.map((m) => (
        <div
          key={m.market}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "8px 0",
            borderBottom: "1px solid var(--border)",
          }}
        >
          <button
            className="switch"
            role="switch"
            aria-checked={m.enabled}
            aria-label={`${m.enabled ? "Desactivar" : "Activar"} ${m.market}`}
            disabled={!running}
            onClick={() => onToggle(m.market, !m.enabled)}
          />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div>
              <strong>{m.market.split(":")[1]}</strong>{" "}
              <span className="cell-sub">{m.market.split(":")[0]}</span>
            </div>
            <div className="cell-sub">
              {m.strategy} · última vela {ago(m.last_bar)}
            </div>
          </div>
          {m.stale && <span className="pill warn">sin datos</span>}
        </div>
      ))}
    </section>
  );
}

export function PositionsCard({ status }: { status: Status }) {
  const rows = status.venues.flatMap((v) => v.positions.map((p) => ({ ...p, currency: v.currency })));
  return (
    <section className="card" aria-labelledby="positions-title">
      <div className="card-head">
        <h2 id="positions-title">Posiciones abiertas</h2>
      </div>
      {rows.length === 0 ? (
        <p className="empty">No hay posiciones abiertas.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Mercado</th>
                <th className="r">Cantidad</th>
                <th className="r">Entrada</th>
                <th className="r">Ahora</th>
                <th className="r">Resultado</th>
                <th className="r">Stop</th>
                <th className="r">Objetivo</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.market}>
                  <td>
                    <strong>{p.symbol}</strong>
                    <div className="cell-sub">{p.strategy}</div>
                  </td>
                  <td className="r num">{qty(p.quantity)}</td>
                  <td className="r num">{price(p.avg_price)}</td>
                  <td className="r num">{price(p.last_price)}</td>
                  <td className={`r num ${tone(p.unrealized)}`}>
                    {money(p.unrealized, p.currency, true)}
                    <div className="cell-sub">{pct(p.unrealized_pct)}</div>
                  </td>
                  <td className="r num down">{price(p.stop_loss)}</td>
                  <td className="r num up">{p.take_profit ? price(p.take_profit) : "por señal"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function TradesCard({ trades }: { trades: Trade[] | undefined }) {
  return (
    <section className="card" aria-labelledby="trades-title">
      <div className="card-head">
        <h2 id="trades-title">Operaciones cerradas</h2>
        {trades && trades.length > 0 && (
          <span className="card-note">
            {trades.filter((t) => t.pnl > 0).length} de {trades.length} ganadoras
          </span>
        )}
      </div>
      {!trades || trades.length === 0 ? (
        <p className="empty">Aún no hay operaciones cerradas.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Salida</th>
                <th>Mercado</th>
                <th className="r">Entrada → salida</th>
                <th className="r">Resultado</th>
                <th>Motivo</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={`${t.market}-${t.exit_time}`}>
                  <td className="num">{when(t.exit_time)}</td>
                  <td>{t.market.split(":")[1]}</td>
                  <td className="r num">
                    {price(t.entry_price)} → {price(t.exit_price)}
                  </td>
                  <td className={`r num ${tone(t.pnl)}`}>
                    {money(t.pnl, t.currency, true)}
                    <div className="cell-sub">{pct(t.return_pct)}</div>
                  </td>
                  <td>{EXIT_REASONS[t.exit_reason] ?? t.exit_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function BacktestsCard({ backtests }: { backtests: Backtest[] | undefined }) {
  return (
    <section className="card" aria-labelledby="backtests-title">
      <div className="card-head">
        <h2 id="backtests-title">Backtests</h2>
        <span className="card-note">netos de comisiones, spread y deslizamiento</span>
      </div>
      {!backtests || backtests.length === 0 ? (
        <p className="empty">
          No hay backtests. Lanza uno con <code>uv run invertio backtest -s puntuacion</code>.
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Estrategia</th>
                <th>Mercado</th>
                <th className="r">Retorno</th>
                <th className="r">Comprar y mantener</th>
                <th className="r">Drawdown máx.</th>
                <th className="r">Operaciones</th>
                <th className="r">Aciertos</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {backtests.flatMap((b) =>
                b.markets.map((m, index) => (
                  <tr key={`${b.id}-${m.market}`}>
                    <td className="num">{index === 0 ? when(b.created_at) : ""}</td>
                    <td>{index === 0 ? b.strategy : ""}</td>
                    <td>
                      {m.market.split(":")[1]}
                      <div className="cell-sub">{m.timeframe}</div>
                    </td>
                    <td className={`r num ${tone(m.total_return_pct)}`}>{pct(m.total_return_pct)}</td>
                    <td className={`r num ${tone(m.buy_and_hold_pct)}`}>{pct(m.buy_and_hold_pct)}</td>
                    <td className="r num">{num(m.max_drawdown_pct)} %</td>
                    <td className="r num">{m.trades}</td>
                    <td className="r num">{num(m.win_rate_pct, 0)} %</td>
                    <td>
                      {index === 0 && (
                        <a href={b.report_url} target="_blank" rel="noreferrer">
                          informe
                        </a>
                      )}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
