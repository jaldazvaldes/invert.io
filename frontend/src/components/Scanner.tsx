import type { ScanRow, ScanState, Score } from "../api";
import { FACTOR_LABELS, pct, points, price, when } from "../format";

function isScore(row: ScanRow): row is Score {
  return "score" in row;
}

interface Props {
  scan: ScanState | undefined;
  onStart: (timeframe: string, all: boolean) => void;
  starting: boolean;
}

export function ScannerCard({ scan, onStart, starting }: Props) {
  const running = scan?.status === "running";
  const progress = scan ? Object.values(scan.progress) : [];
  const done = progress.reduce((sum, [d]) => sum + d, 0);
  const total = progress.reduce((sum, [, t]) => sum + t, 0);
  const rows = scan?.rows ?? [];
  const scored = rows.filter(isScore);
  const meets = scored.filter((r) => r.meets).length;

  return (
    <section className="card" aria-labelledby="scanner-title">
      <div className="card-head">
        <h2 id="scanner-title">Escáner</h2>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            className="btn btn-primary"
            disabled={running || starting}
            onClick={() => onStart("1h", true)}
          >
            {running ? "Escaneando…" : "Escanear todos los pares (1 h)"}
          </button>
          <button className="btn" disabled={running || starting} onClick={() => onStart("5m", false)}>
            Solo mis mercados (5 min)
          </button>
        </div>
      </div>
      <p className="card-note" style={{ marginTop: -4 }}>
        Aplica la estrategia de puntuación a cada mercado. Es el resultado de reglas fijas, no una
        recomendación: compruébalas en backtest antes de fiarte.
      </p>
      {running && (
        <>
          <div className="progress" aria-label="Progreso del escaneo">
            <span style={{ width: total ? `${(done / total) * 100}%` : "5%" }} />
          </div>
          <p className="card-note">
            {done} de {total || "…"} mercados (Revolut X permite 1 petición por segundo)
          </p>
        </>
      )}
      {scan?.status === "error" && <p className="down">Error: {scan.error}</p>}
      {scan?.skipped.map((s) => (
        <p className="card-note" key={s.venue}>
          {s.venue}: {s.reason}
        </p>
      ))}
      {scan?.status === "done" && (
        <p className="card-note">
          {scored.length} mercados puntuados · {meets} cumplen · velas {scan.timeframe} ·{" "}
          {when(scan.finished_at)}
        </p>
      )}
      {scan?.status === "idle" && <p className="empty">Pulsa un botón para escanear.</p>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Mercado</th>
                <th className="r">Nota</th>
                <th>Factores</th>
                <th className="r">ATR</th>
                <th className="r">Precio</th>
                <th className="r">Stop</th>
                <th className="r">Objetivo</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) =>
                isScore(row) ? (
                  <tr key={row.market}>
                    <td className="num muted">{index + 1}</td>
                    <td>
                      <strong>{row.symbol}</strong>
                    </td>
                    <td className="r num">
                      <strong>{points(row.score)}</strong>
                    </td>
                    <td>
                      <span
                        className="mini-factors"
                        title={row.factors
                          .map((f) => `${FACTOR_LABELS[f]} ${points(row.points[f])}/${row.max_points[f]}`)
                          .join(" · ")}
                      >
                        {row.factors.map((f) => (
                          <span
                            key={f}
                            style={{ height: `${Math.max(10, (row.points[f] / row.max_points[f]) * 100)}%` }}
                          />
                        ))}
                      </span>
                    </td>
                    <td className="r num">{pct(row.atr_pct, false)}</td>
                    <td className="r num">{price(row.close)}</td>
                    <td className="r num down">{price(row.stop_loss)}</td>
                    <td className="r num up">{price(row.take_profit)}</td>
                    <td>
                      {row.meets ? (
                        <span className="pill ok">cumple</span>
                      ) : !row.tradable ? (
                        <span className="pill warn">volatilidad insuficiente</span>
                      ) : null}
                    </td>
                  </tr>
                ) : (
                  <tr key={row.market}>
                    <td className="num muted">{index + 1}</td>
                    <td>{row.market.split(":")[1]}</td>
                    <td colSpan={7} className="muted">
                      {row.note}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
