"""Orquestación del laboratorio: entrenamiento (elegir parámetros) y test (juzgar)."""

from __future__ import annotations

import os
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from invertio.config.app_config import AppConfig, InstrumentRules, SimulationConfig
from invertio.core.timeframes import timeframe_delta
from invertio.data.store import BarStore, DatasetInfo
from invertio.lab import worker
from invertio.lab.config import LabConfig
from invertio.lab.worker import LabTask
from invertio.strategies import STRATEGIES

TOP_PER_GROUP = 3  # combinaciones de entrenamiento que se pasan también por el test

type Key = tuple[str, str, tuple[tuple[str, Any], ...]]  # (estrategia, timeframe, parámetros)


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Resumen de una combinación en un periodo, sobre todas las monedas."""

    symbols: int
    median_return: float
    mean_return: float
    pct_positive: float
    median_buy_and_hold: float
    median_drawdown: float
    avg_trades: float
    errors: int
    # Ganancia media por operación (neta de costes), mediana entre monedas con operaciones.
    # No depende del tamaño de la posición: mide la calidad de la regla en sí.
    median_trade_pct: float = 0.0

    @classmethod
    def of(cls, rows: list[dict[str, Any]]) -> Aggregate:
        ok = [r for r in rows if "error" not in r]
        if not ok:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, len(rows))
        returns = [r["total_return_pct"] for r in ok]
        return cls(
            symbols=len(ok),
            median_return=statistics.median(returns),
            mean_return=statistics.fmean(returns),
            pct_positive=sum(1 for x in returns if x > 0) / len(ok) * 100,
            median_buy_and_hold=statistics.median(r["buy_and_hold_pct"] for r in ok),
            median_drawdown=statistics.median(r["max_drawdown_pct"] for r in ok),
            avg_trades=statistics.fmean(r["trades"] for r in ok),
            errors=len(rows) - len(ok),
            median_trade_pct=statistics.median(
                [r["avg_trade_pct"] for r in ok if r["trades"] > 0] or [0.0]
            ),
        )


@dataclass(slots=True)
class Candidate:
    strategy: str
    timeframe: str
    params: dict[str, Any]
    train_rank: int  # 1 = la mejor del entrenamiento (la que decide el veredicto)
    train: Aggregate
    test: Aggregate | None = None
    test_rows: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LabResult:
    started_at: datetime
    finished_at: datetime
    test_start: datetime
    symbols_train: list[str]
    symbols_test: list[str]
    combinations: int
    backtests: int
    candidates: list[Candidate]
    spreads: dict[str, float]
    train_table: list[dict[str, Any]]  # todas las combinaciones con su resumen de entrenamiento


def lab_app_config(
    base: AppConfig,
    venue_id: str,
    symbols: list[str],
    rules: dict[str, InstrumentRules],
    spreads: dict[str, float],
) -> AppConfig:
    """Config del venue simulado con todas las monedas, su precisión y su spread real."""
    venue = base.venue(venue_id)
    simulation = {
        s: SimulationConfig(
            spread_pct=max(
                spreads.get(s, venue.simulation.spread_pct), venue.simulation.spread_pct
            ),
            slippage_pct=venue.simulation.slippage_pct,
            limit_ttl_bars=venue.simulation.limit_ttl_bars,
        )
        for s in symbols
    }
    venue = venue.model_copy(
        update={
            "symbols": symbols,
            "instrument_overrides": {**venue.instrument_overrides, **rules},
            "simulation_overrides": simulation,
        }
    )
    return base.model_copy(update={"venues": [venue]})


def _warmup(strategy_id: str, params: dict[str, Any], timeframe: str) -> timedelta:
    cls = STRATEGIES[strategy_id]
    strategy = cls(cls.params_model.model_validate(params))
    return (strategy.warmup_bars + 5) * timeframe_delta(timeframe)


def plan_train(
    lab: LabConfig, datasets: dict[str, DatasetInfo], test_start: datetime
) -> tuple[list[LabTask], list[str]]:
    tasks = []
    eligible: set[str] = set()
    for strategy_id, grid in lab.strategies.items():
        for timeframe in grid.timeframes:
            for params in lab.combinations(strategy_id):
                warmup = _warmup(strategy_id, params, timeframe)
                for symbol, info in datasets.items():
                    trade_from = info.first + warmup
                    if test_start - trade_from < timedelta(days=lab.min_train_days):
                        continue
                    eligible.add(symbol)
                    tasks.append(
                        LabTask(strategy_id, timeframe, tuple(sorted(params.items())), symbol,
                                "train", info.first, trade_from, test_start)
                    )  # fmt: skip
    # Ordenadas por moneda: cada proceso reutiliza las velas que ya tiene en memoria.
    tasks.sort(key=lambda t: (t.symbol, t.timeframe, t.strategy_id))
    return tasks, sorted(eligible)


def plan_test(
    lab: LabConfig,
    datasets: dict[str, DatasetInfo],
    candidates: list[Candidate],
    test_start: datetime,
    end: datetime,
) -> tuple[list[LabTask], list[str]]:
    tasks = []
    eligible: set[str] = set()
    for c in candidates:
        warmup = _warmup(c.strategy, c.params, c.timeframe)
        for symbol, info in datasets.items():
            if info.last - test_start < timedelta(days=lab.min_test_days):
                continue
            eligible.add(symbol)
            tasks.append(
                LabTask(c.strategy, c.timeframe, tuple(sorted(c.params.items())), symbol, "test",
                        test_start - warmup, test_start, end)
            )  # fmt: skip
    tasks.sort(key=lambda t: (t.symbol, t.timeframe, t.strategy_id))
    return tasks, sorted(eligible)


def _execute(
    tasks: list[LabTask],
    workers: int,
    initargs: tuple[Any, ...],
    on_progress: Callable[[int, int], None] | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    iterator: Iterator[dict[str, Any]]
    if workers <= 0:  # en el propio proceso (tests)
        worker.init(*initargs)
        iterator = map(worker.run_task, tasks)
        results = _collect(iterator, len(tasks), on_progress)
        return results
    with ProcessPoolExecutor(
        max_workers=workers, initializer=worker.init, initargs=initargs
    ) as executor:
        chunksize = max(1, min(32, len(tasks) // (workers * 8) or 1))
        iterator = executor.map(worker.run_task, tasks, chunksize=chunksize)
        results = _collect(iterator, len(tasks), on_progress)
    return results


def _collect(
    iterator: Iterable[dict[str, Any]], total: int, on_progress: Callable[[int, int], None] | None
) -> list[dict[str, Any]]:
    results = []
    for index, row in enumerate(iterator, start=1):
        results.append(row)
        if on_progress is not None and (index % 25 == 0 or index == total):
            on_progress(index, total)
    return results


def _key(row: dict[str, Any]) -> Key:
    return (row["strategy"], row["timeframe"], tuple(sorted(row["params"].items())))


def judge(candidate: Candidate, lab: LabConfig) -> None:
    v = lab.verdict
    test, train = candidate.test, candidate.train
    reasons = []
    if test is None or test.symbols == 0:
        candidate.verdict, candidate.reasons = "sin datos", ["no hay monedas con test suficiente"]
        return
    if train.median_return <= v.min_median_return_pct:
        reasons.append(f"entrenamiento negativo ({train.median_return:+.1f} %)")
    if test.median_return <= v.min_median_return_pct:
        reasons.append(f"mediana en test {test.median_return:+.1f} %")
    if test.pct_positive < v.min_positive_pct:
        reasons.append(f"solo {test.pct_positive:.0f} % de monedas en positivo")
    if test.avg_trades < v.min_avg_trades:
        reasons.append(f"pocas operaciones ({test.avg_trades:.1f} por moneda)")
    candidate.reasons = reasons
    candidate.verdict = "aprueba" if not reasons else "no aprueba"


def run_lab(
    lab: LabConfig,
    app_config: AppConfig,
    bars_root: Path,
    *,
    workers: int | None = None,
    now: datetime | None = None,
    only_strategies: list[str] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    spreads: dict[str, float] | None = None,
) -> LabResult:
    started = datetime.now(UTC)
    now = now or started
    if only_strategies:
        lab = lab.model_copy(
            update={"strategies": {k: v for k, v in lab.strategies.items() if k in only_strategies}}
        )
    store = BarStore(bars_root)
    venue = app_config.venue(lab.venue)
    datasets = {s: store.info(lab.source, s, lab.base_timeframe) for s in venue.symbols}
    available = {s: info for s, info in datasets.items() if info is not None}
    test_start = now - timedelta(days=lab.test_days)
    workers = (os.cpu_count() or 2) - 1 if workers is None else workers
    initargs = (app_config, bars_root, lab.source, lab.venue, lab.base_timeframe, lab.capital)

    train_tasks, symbols_train = plan_train(lab, available, test_start)
    if on_phase:
        on_phase(f"Entrenamiento: {len(train_tasks)} backtests en {len(symbols_train)} monedas")
    train_rows = _execute(train_tasks, workers, initargs, on_progress)

    grouped: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for row in train_rows:
        grouped[_key(row)].append(row)
    table = []
    by_group: dict[tuple[str, str], list[tuple[Key, Aggregate]]] = defaultdict(list)
    for key, rows in grouped.items():
        agg = Aggregate.of(rows)
        by_group[(key[0], key[1])].append((key, agg))
        table.append(
            {"strategy": key[0], "timeframe": key[1], "params": dict(key[2]), **asdict(agg)}
        )

    candidates: list[Candidate] = []
    for (strategy_id, timeframe), items in by_group.items():
        # Se priorizan las combinaciones con operaciones suficientes para juzgarlas.
        items.sort(
            key=lambda item: (
                item[1].avg_trades >= lab.verdict.min_avg_trades,
                item[1].median_return,
                item[1].pct_positive,
            ),
            reverse=True,
        )
        for rank, (key, agg) in enumerate(items[:TOP_PER_GROUP], start=1):
            candidates.append(Candidate(strategy_id, timeframe, dict(key[2]), rank, agg))

    test_tasks, symbols_test = plan_test(lab, available, candidates, test_start, now)
    if on_phase:
        on_phase(f"Test: {len(test_tasks)} backtests en {len(symbols_test)} monedas")
    test_rows = _execute(test_tasks, workers, initargs, on_progress)
    by_key: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for row in test_rows:
        by_key[_key(row)].append(row)
    for c in candidates:
        rows = by_key.get((c.strategy, c.timeframe, tuple(sorted(c.params.items()))), [])
        c.test_rows = rows
        c.test = Aggregate.of(rows)
        judge(c, lab)

    candidates.sort(
        key=lambda c: (c.train_rank == 1, c.test.median_return if c.test else -1e9), reverse=True
    )
    return LabResult(
        started_at=started,
        finished_at=datetime.now(UTC),
        test_start=test_start,
        symbols_train=symbols_train,
        symbols_test=symbols_test,
        combinations=len(grouped),
        backtests=len(train_rows) + len(test_rows),
        candidates=candidates,
        spreads=spreads or {},
        train_table=table,
    )
