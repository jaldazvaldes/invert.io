import type { LabSummary } from "../api";
import { day, pct } from "../format";

function params(p: Record<string, unknown>): string {
  return Object.entries(p)
    .map(([k, v]) => `${k}=${v === null ? "—" : String(v)}`)
    .join(", ");
}

export function LabCard({ lab }: { lab: LabSummary | null | undefined }) {
  const rows = [...(lab?.candidates ?? [])].sort(
    (a, b) => (b.test?.median_return ?? -1e9) - (a.test?.median_return ?? -1e9),
  );
  const approved = rows.filter((c) => c.verdict === "aprueba").length;
  return (
    <section className="card" aria-labelledby="lab-title">
      <div className="card-head">
        <h2 id="lab-title">Laboratorio</h2>
        {lab && (
          <span className="card-note">
            test desde {day(lab.test_start)} · {lab.symbols_test} monedas · {lab.backtests} backtests ·{" "}
            <a href={lab.report_url} target="_blank" rel="noreferrer">
              informe completo
            </a>
          </span>
        )}
      </div>
      {!lab ? (
        <p className="empty">
          Aún no hay resultados. Lanza el laboratorio con <code>uv run invertio lab run</code>.
        </p>
      ) : (
        <>
          <p className="card-note" style={{ marginTop: -4 }}>
            Cada estrategia se optimiza en el pasado y se juzga en el último año, que no ha visto.{" "}
            {approved > 0 ? (
              <strong className="up">{approved} aprueba(n).</strong>
            ) : (
              <strong className="down">Ninguna aprueba todavía.</strong>
            )}
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Estrategia</th>
                  <th className="r">Test mediana</th>
                  <th className="r">Monedas +</th>
                  <th className="r">Ops/moneda</th>
                  <th className="r">Media/op.</th>
                  <th className="r">Comprar y mantener</th>
                  <th>Veredicto</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={`${c.strategy}-${c.timeframe}`}>
                    <td>
                      <strong>{c.strategy}</strong> <span className="cell-sub">{c.timeframe}</span>
                      <div className="cell-sub">{params(c.params)}</div>
                    </td>
                    <td className={`r num ${(c.test?.median_return ?? 0) > 0 ? "up" : "down"}`}>
                      {c.test ? pct(c.test.median_return) : "—"}
                    </td>
                    <td className="r num">{c.test ? `${Math.round(c.test.pct_positive)} %` : "—"}</td>
                    <td className="r num">{c.test ? c.test.avg_trades.toFixed(1).replace(".", ",") : "—"}</td>
                    <td className="r num">
                      {c.test?.median_trade_pct != null ? pct(c.test.median_trade_pct) : "—"}
                    </td>
                    <td className="r num">{c.test ? pct(c.test.median_buy_and_hold) : "—"}</td>
                    <td title={c.reasons.join("; ")}>
                      <span className={`pill ${c.verdict === "aprueba" ? "ok" : "bad"}`}>{c.verdict}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
