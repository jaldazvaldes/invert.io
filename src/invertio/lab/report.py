"""Informe del laboratorio: tabla en consola y ficheros en data/lab/<ejecución>/."""

from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from invertio.lab.config import LabConfig
from invertio.lab.runner import Candidate, LabResult


def _params(params: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _pct(value: float) -> str:
    return f"{value:+.1f} %".replace(".", ",")


def print_lab(console: Console, result: LabResult, lab: LabConfig) -> None:
    v = lab.verdict
    console.print(
        f"\n[bold]Laboratorio[/bold] · test desde {result.test_start:%Y-%m-%d} · "
        f"{result.combinations} combinaciones · {result.backtests} backtests · "
        f"{len(result.symbols_train)} monedas en entrenamiento, {len(result.symbols_test)} en test"
    )
    console.print(
        f"[dim]Aprueba si en test: mediana > {v.min_median_return_pct:g} %, "
        f"≥ {v.min_positive_pct:g} % de monedas en positivo y ≥ {v.min_avg_trades:g} "
        "operaciones por moneda (y el entrenamiento también fue positivo).[/dim]"
    )
    table = Table(
        title="Mejor combinación de cada estrategia (elegida en entrenamiento, juzgada en test)"
    )
    for column, justify in (
        ("estrategia", "left"),
        ("velas", "left"),
        ("parámetros", "left"),
        ("entren. mediana", "right"),
        ("test mediana", "right"),
        ("test % positivas", "right"),
        ("test ops/moneda", "right"),
        ("test media/op.", "right"),
        ("comprar y mantener", "right"),
        ("drawdown med.", "right"),
        ("veredicto", "left"),
    ):
        table.add_column(column, justify=justify)  # type: ignore[arg-type]
    for c in result.candidates:
        if c.train_rank != 1 or c.test is None:
            continue
        color = "green" if c.verdict == "aprueba" else "red"
        table.add_row(
            c.strategy,
            c.timeframe,
            _params(c.params),
            _pct(c.train.median_return),
            _pct(c.test.median_return),
            f"{c.test.pct_positive:.0f} %",
            f"{c.test.avg_trades:.1f}",
            _pct(c.test.median_trade_pct),
            _pct(c.test.median_buy_and_hold),
            f"{c.test.median_drawdown:.1f} %".replace(".", ","),
            f"[{color}]{c.verdict}[/{color}]",
        )
    console.print(table)
    approved = [c for c in result.candidates if c.train_rank == 1 and c.verdict == "aprueba"]
    if approved:
        console.print(f"[green]{len(approved)} estrategia(s) aprueban el laboratorio.[/green]")
    else:
        console.print("[yellow]Ninguna estrategia aprueba el laboratorio.[/yellow]")


def save_lab(root: Path, result: LabResult, lab: LabConfig) -> Path:
    folder = root / result.started_at.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "test_start": result.test_start.isoformat(),
        "verdict_rules": lab.verdict.model_dump(),
        "symbols_train": result.symbols_train,
        "symbols_test": result.symbols_test,
        "combinations": result.combinations,
        "backtests": result.backtests,
        "spreads_pct": result.spreads,
        "candidates": [
            {
                "strategy": c.strategy,
                "timeframe": c.timeframe,
                "params": c.params,
                "train_rank": c.train_rank,
                "train": asdict(c.train),
                "test": asdict(c.test) if c.test else None,
                "verdict": c.verdict,
                "reasons": c.reasons,
            }
            for c in result.candidates
        ],
    }
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    (folder / "train_all.json").write_text(
        json.dumps(result.train_table, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
    )
    with (folder / "test_by_symbol.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["estrategia", "velas", "parametros", "rango_entrenamiento", "moneda", "retorno_pct",
             "comprar_y_mantener_pct", "drawdown_pct", "operaciones", "aciertos_pct", "error"]
        )  # fmt: skip
        for c in result.candidates:
            for row in c.test_rows:
                writer.writerow(
                    [c.strategy, c.timeframe, _params(c.params), c.train_rank, row["symbol"],
                     row.get("total_return_pct"), row.get("buy_and_hold_pct"),
                     row.get("max_drawdown_pct"), row.get("trades"), row.get("win_rate_pct"),
                     row.get("error", "")]
                )  # fmt: skip
    (folder / "report.html").write_text(_html(result, lab), encoding="utf-8")
    return folder


def _row(c: Candidate) -> str:
    test = c.test
    tone = "ok" if c.verdict == "aprueba" else "bad"
    reasons = "; ".join(c.reasons) or "cumple todos los criterios"
    if test is None:
        return ""
    return (
        f"<tr><td>{html.escape(c.strategy)}</td><td>{c.timeframe}</td>"
        f"<td class='p'>{html.escape(_params(c.params))}</td><td>{c.train_rank}</td>"
        f"<td>{_pct(c.train.median_return)}</td><td>{_pct(test.median_return)}</td>"
        f"<td>{test.pct_positive:.0f} %</td><td>{test.avg_trades:.1f}</td>"
        f"<td>{_pct(test.median_trade_pct)}</td>"
        f"<td>{_pct(test.median_buy_and_hold)}</td><td>{test.median_drawdown:.1f} %</td>"
        f"<td class='{tone}' title='{html.escape(reasons)}'>{html.escape(c.verdict)}</td></tr>"
    )


def _html(result: LabResult, lab: LabConfig) -> str:
    v = lab.verdict
    best = [c for c in result.candidates if c.train_rank == 1]
    others = [c for c in result.candidates if c.train_rank != 1]
    head = (
        "<tr><th>Estrategia</th><th>Velas</th><th>Parámetros</th><th>Rango entren.</th>"
        "<th>Entren. mediana</th><th>Test mediana</th><th>Test % positivas</th>"
        "<th>Ops/moneda</th><th>Media por op.</th><th>Comprar y mantener</th>"
        "<th>Drawdown med.</th>"
        "<th>Veredicto</th></tr>"
    )
    best_rows = "".join(_row(c) for c in best)
    other_rows = "".join(_row(c) for c in others)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laboratorio de estrategias</title>
<style>
:root {{ --bg:#fff; --fg:#111827; --muted:#6b7280; --line:#e5e7eb; --ok:#15803d; --bad:#b91c1c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f172a; --fg:#e5e7eb; --muted:#94a3b8;
  --line:#334155; --ok:#4ade80; --bad:#f87171; }} }}
body {{ background:var(--bg); color:var(--fg); margin:0 auto; max-width:1200px; padding:24px 16px;
  font:14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
h1 {{ font-size:1.4rem; margin:0 0 4px; }} h2 {{ font-size:1.05rem; margin:28px 0 8px; }}
p {{ color:var(--muted); margin:4px 0; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ padding:6px 8px; border-bottom:1px solid var(--line); text-align:right;
  white-space:nowrap; }}
th:nth-child(-n+3), td:nth-child(-n+3) {{ text-align:left; }}
td.p {{ white-space:normal; min-width:220px; color:var(--muted); }}
.ok {{ color:var(--ok); font-weight:600; }} .bad {{ color:var(--bad); font-weight:600; }}
</style></head><body>
<h1>Laboratorio de estrategias</h1>
<p>Entrenamiento hasta {result.test_start:%d/%m/%Y}; test desde entonces (fuera de muestra).
{result.combinations} combinaciones · {result.backtests} backtests ·
{len(result.symbols_train)} monedas en entrenamiento y {len(result.symbols_test)} en test.</p>
<p>Aprueba si en test: mediana &gt; {v.min_median_return_pct:g} %, al menos
{v.min_positive_pct:g} % de monedas en positivo y {v.min_avg_trades:g} o más operaciones por
moneda, con el entrenamiento también positivo. Resultados netos de comisiones, spread real de
cada moneda y deslizamiento. Rendimientos simulados: no garantizan resultados futuros.</p>
<h2>Mejor combinación de cada estrategia</h2>
<div class="wrap"><table><thead>{head}</thead><tbody>{best_rows}</tbody></table></div>
<h2>Siguientes del entrenamiento (para ver si el ranking se mantiene en test)</h2>
<div class="wrap"><table><thead>{head}</thead><tbody>{other_rows}</tbody></table></div>
</body></html>
"""
