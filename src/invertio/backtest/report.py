"""Informes de backtest: tabla comparativa en consola y ficheros en data/reports/<ejecución>/."""

from __future__ import annotations

import csv
import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from invertio.backtest.engine import BacktestResult
from invertio.backtest.metrics import Metrics


@dataclass(frozen=True, slots=True)
class MarketReport:
    result: BacktestResult
    metrics: Metrics
    data_source: str
    quote_currency: str

    @property
    def label(self) -> str:
        return f"{self.result.venue}:{self.result.symbol}"


def _signed(value: float, decimals: int = 2) -> str:
    return f"{value:+.{decimals}f}"


def print_comparison(console: Console, reports: list[MarketReport]) -> None:
    table = Table(title="Backtest: resultado neto de comisiones, spread y deslizamiento")
    for column, justify in (
        ("mercado", "left"),
        ("retorno %", "right"),
        ("comprar y mantener %", "right"),
        ("drawdown máx %", "right"),
        ("Sharpe", "right"),
        ("operaciones", "right"),
        ("aciertos %", "right"),
        ("profit factor", "right"),
        ("comisiones", "right"),
        ("en mercado %", "right"),
    ):
        table.add_column(column, justify=justify)  # type: ignore[arg-type]
    for report in reports:
        m = report.metrics
        color = "green" if m.total_return_pct > 0 else "red"
        table.add_row(
            report.label,
            f"[{color}]{_signed(m.total_return_pct)}[/{color}]",
            _signed(m.buy_and_hold_pct),
            f"{m.max_drawdown_pct:.2f}",
            f"{m.sharpe:.2f}",
            str(m.trades),
            f"{m.win_rate_pct:.0f}",
            "—" if m.profit_factor is None else f"{m.profit_factor:.2f}",
            f"{m.fees_paid:.2f} {report.quote_currency}",
            f"{m.exposure_pct:.0f}",
        )
    console.print(table)
    for report in reports:
        rejected = report.result.rejections
        if rejected:
            reasons = ", ".join(f"{reason} ({n})" for reason, n in rejected.most_common(4))
            console.print(f"[dim]{report.label} · señales rechazadas: {reasons}[/dim]")


def save_reports(root: Path, strategy_id: str, reports: list[MarketReport]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder = root / f"{stamp}_{strategy_id}"
    folder.mkdir(parents=True, exist_ok=True)

    summary = [
        {
            "market": r.label,
            "data_source": r.data_source,
            "timeframe": r.result.timeframe,
            "from": r.result.equity_curve[0][0].isoformat() if r.result.equity_curve else None,
            "to": r.result.equity_curve[-1][0].isoformat() if r.result.equity_curve else None,
            "initial_cash": str(r.result.initial_cash),
            "final_equity": str(r.result.final_equity),
            "signals": r.result.signals,
            "approved": r.result.approved,
            "rejections": dict(r.result.rejections),
            "orders": r.result.orders,
            "unfilled_orders": r.result.unfilled_orders,
            "metrics": r.metrics.as_dict(),
        }
        for r in reports
    ]
    (folder / "summary.json").write_text(
        json.dumps({"strategy": strategy_id, "markets": summary}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    with (folder / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["mercado", "entrada", "salida", "cantidad", "precio_entrada", "precio_salida",
             "pnl_neto", "comisiones", "retorno_pct", "motivo_salida"]
        )  # fmt: skip
        for r in reports:
            for t in r.result.trades:
                writer.writerow(
                    [r.label, t.entry_time.isoformat(), t.exit_time.isoformat(), t.quantity,
                     t.entry_price, t.exit_price, t.pnl, t.fees, f"{t.return_pct:.4f}",
                     t.exit_reason]
                )  # fmt: skip

    with (folder / "equity.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["mercado", "ts", "equity"])
        for r in reports:
            for ts, equity in r.result.equity_curve:
                writer.writerow([r.label, ts.isoformat(), equity])

    (folder / "report.html").write_text(_html(strategy_id, reports), encoding="utf-8")
    return folder


_COLORS = ["#2563eb", "#d97706", "#059669", "#db2777"]


def _svg_curves(reports: list[MarketReport], width: int = 900, height: int = 300) -> str:
    """Curvas de equity normalizadas a % de retorno, en un único SVG sin dependencias."""
    series = []
    for r in reports:
        curve = r.result.equity_curve
        if len(curve) < 2:
            continue
        step = max(1, len(curve) // width)
        points = [*curve[::step], curve[-1]]
        t0, t1 = points[0][0].timestamp(), points[-1][0].timestamp()
        values = [float(e / r.result.initial_cash - 1) * 100 for _, e in points]
        series.append(([(t.timestamp() - t0) / (t1 - t0 or 1) for t, _ in points], values))
    if not series:
        return "<p>Sin datos suficientes para la gráfica.</p>"
    lo = min(min(v) for _, v in series)
    hi = max(max(v) for _, v in series)
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = hi - lo or 1.0
    pad = 30

    def y(value: float) -> float:
        return pad + (hi - value) / span * (height - 2 * pad)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Curvas de equity">',
        f'<line x1="{pad}" x2="{width - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>',
        f'<text x="{pad}" y="{y(hi) - 8:.1f}" class="axis">{hi:+.1f} %</text>',
        f'<text x="{pad}" y="{y(lo) + 16:.1f}" class="axis">{lo:+.1f} %</text>',
    ]
    for index, (xs, values) in enumerate(series):
        coords = " ".join(
            f"{pad + x * (width - 2 * pad):.1f},{y(v):.1f}" for x, v in zip(xs, values, strict=True)
        )
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{_COLORS[index % len(_COLORS)]}" '
            'stroke-width="1.6"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _html(strategy_id: str, reports: list[MarketReport]) -> str:
    rows = []
    for index, r in enumerate(reports):
        m = r.metrics
        color = _COLORS[index % len(_COLORS)]
        pf = "—" if m.profit_factor is None else f"{m.profit_factor:.2f}"
        rows.append(
            f"<tr><td><span class='dot' style='background:{color}'></span>"
            f"{html.escape(r.label)}</td><td>{m.total_return_pct:+.2f}</td>"
            f"<td>{m.buy_and_hold_pct:+.2f}</td><td>{m.max_drawdown_pct:.2f}</td>"
            f"<td>{m.sharpe:.2f}</td><td>{m.trades}</td><td>{m.win_rate_pct:.0f}</td>"
            f"<td>{pf}</td><td>{m.fees_paid:.2f} {html.escape(r.quote_currency)}</td>"
            f"<td>{html.escape(r.data_source)}</td></tr>"
        )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest {html.escape(strategy_id)}</title>
<style>
:root {{ --bg:#fff; --fg:#111827; --muted:#6b7280; --line:#e5e7eb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f172a; --fg:#e5e7eb; --muted:#94a3b8;
  --line:#334155; }} }}
body {{ background:var(--bg); color:var(--fg); margin:0 auto; max-width:960px; padding:24px 16px;
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }} p.note {{ color:var(--muted); margin:0 0 20px; }}
svg {{ width:100%; height:auto; border:1px solid var(--line); border-radius:8px; }}
.zero {{ stroke:var(--muted); stroke-dasharray:4 4; }}
.axis {{ fill:var(--muted); font-size:12px; }}
.table-wrap {{ overflow-x:auto; margin-top:20px; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:6px 10px; border-bottom:1px solid var(--line); text-align:right;
  white-space:nowrap; }}
th:first-child, td:first-child {{ text-align:left; }}
.dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px; }}
</style></head><body>
<h1>Backtest · {html.escape(strategy_id)}</h1>
<p class="note">Resultados netos de comisiones, spread y deslizamiento. Rendimientos pasados
simulados: no garantizan resultados futuros.</p>
{_svg_curves(reports)}
<div class="table-wrap"><table>
<thead><tr><th>Mercado</th><th>Retorno %</th><th>Comprar y mantener %</th><th>Drawdown máx %</th>
<th>Sharpe</th><th>Operaciones</th><th>Aciertos %</th><th>Profit factor</th><th>Comisiones</th>
<th>Datos</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
</body></html>
"""
