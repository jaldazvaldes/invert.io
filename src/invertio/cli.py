"""Línea de comandos de invert.io."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from invertio import __version__
from invertio.config import AppConfig, Settings, load_app_config
from invertio.config.live_config import LiveConfig, load_live_config
from invertio.core.models import TradingMode
from invertio.data.download import Market, parse_market
from invertio.data.store import BarStore
from invertio.logs import configure_logging

app = typer.Typer(
    help="invert.io: trading intradía por señales (cripto + acciones).",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # nunca volcar variables locales: podrían ser secretos
)
config_app = typer.Typer(help="Configuración (.env + config/*.yaml).", no_args_is_help=True)
db_app = typer.Typer(help="Base de datos SQLite.", no_args_is_help=True)
data_app = typer.Typer(help="Datos de mercado históricos.", no_args_is_help=True)
paper_app = typer.Typer(help="Resultados e historial del modo paper.", no_args_is_help=True)
telegram_app = typer.Typer(help="Configuración del bot de Telegram.", no_args_is_help=True)
lab_app = typer.Typer(
    help="Laboratorio de estrategias (optimizar y validar).", no_args_is_help=True
)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(data_app, name="data")
app.add_typer(paper_app, name="paper")
app.add_typer(telegram_app, name="telegram")
app.add_typer(lab_app, name="lab")

console = Console()


def _load_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        console.print("[red]Error en .env / variables de entorno:[/red]")
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            console.print(f"  • {location}: {error['msg']}")
        raise typer.Exit(code=1) from None
    configure_logging(settings.log_level, json=settings.log_json)
    return settings


def _load_app_config(settings: Settings) -> AppConfig:
    try:
        return load_app_config(settings.config_dir)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error en la configuración YAML:[/red] {exc}")
        raise typer.Exit(code=1) from None


@app.command()
def version() -> None:
    """Muestra la versión."""
    console.print(f"invert.io {__version__}")


@config_app.command("check")
def config_check() -> None:
    """Valida .env y config/*.yaml y muestra un resumen (sin revelar secretos)."""
    settings = _load_settings()
    config = _load_app_config(settings)

    console.print(f"Modo de trading: [bold]{settings.trading_mode.value}[/bold]")
    console.print(f"Datos: {settings.data_dir.resolve()}  ·  timeframe {config.data.timeframe}")

    venues = Table(title="Venues")
    for column in ("id", "tipo", "activo", "símbolos", "maker %", "taker %", "divisa %"):
        venues.add_column(column)
    for venue in config.venues:
        venues.add_row(
            venue.id,
            venue.asset_class.value,
            "sí" if venue.enabled else "no",
            ", ".join(venue.symbols),
            f"{venue.fees.maker_pct:g}",
            f"{venue.fees.taker_pct:g}",
            f"{venue.fees.fx_pct:g}",
        )
    console.print(venues)

    risk = Table(title="Límites de riesgo")
    risk.add_column("parámetro")
    risk.add_column("valor")
    for name, value in config.risk.model_dump().items():
        risk.add_row(name, str(value))
    console.print(risk)

    credentials = Table(title="Credenciales (.env)")
    credentials.add_column("servicio")
    credentials.add_column("estado")
    for service, configured in settings.secrets_status().items():
        credentials.add_row(service, "[green]configurada[/green]" if configured else "falta")
    console.print(credentials)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Crea o actualiza la base de datos a la última versión del esquema."""
    from invertio.persistence.db import upgrade_db

    settings = _load_settings()
    upgrade_db(settings)
    console.print(f"Base de datos al día: {settings.db_path.resolve()}")


MarketsOption = Annotated[
    list[str],
    typer.Option(
        "--market",
        "-m",
        help="Mercado venue:SÍMBOLO (repetible). Por defecto, todos los de config/app.yaml.",
    ),
]
TimeframeOption = Annotated[
    str | None, typer.Option("--tf", help="Timeframe (1m, 5m, 15m…). Por defecto el de app.yaml.")
]


def _markets(config: AppConfig, specs: list[str]) -> list[Market]:
    if not specs:
        specs = [f"{v.id}:{s}" for v in config.venues if v.enabled for s in v.symbols]
    try:
        return [parse_market(config, spec) for spec in specs]
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


def _parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        console.print(f"[red]Fecha no válida {value!r}: usa AAAA-MM-DD[/red]")
        raise typer.Exit(code=1) from None


def _window(days: int, date_from: str | None, date_to: str | None) -> tuple[datetime, datetime]:
    end = _parse_date(date_to) or datetime.now(UTC)
    start = _parse_date(date_from) or end - timedelta(days=days)
    if start >= end:
        console.print("[red]El inicio debe ser anterior al final[/red]")
        raise typer.Exit(code=1)
    return start, end


@data_app.command("download")
def data_download(
    markets: MarketsOption = [],  # noqa: B006 (typer necesita un default mutable aquí)
    timeframe: TimeframeOption = None,
    days: Annotated[int, typer.Option(help="Días hacia atrás desde hoy.")] = 90,
    date_from: Annotated[str | None, typer.Option("--from", help="Inicio AAAA-MM-DD.")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Fin AAAA-MM-DD.")] = None,
) -> None:
    """Descarga velas históricas (cripto: datos públicos de OKX; acciones: Alpaca)."""
    from invertio.data.download import download_market

    settings = _load_settings()
    config = _load_app_config(settings)
    tf = timeframe or config.data.timeframe
    start, end = _window(days, date_from, date_to)
    store = BarStore(settings.data_dir / "bars")

    async def run() -> None:
        for market in _markets(config, markets):
            with console.status(f"Descargando {market.label} {tf}…") as status:

                def progress(count: int, label: str = market.label) -> None:
                    status.update(f"Descargando {label} {tf}… {count} velas")

                try:
                    source, downloaded, total = await download_market(
                        settings, config, store, market, tf, start, end, on_progress=progress
                    )
                except PermissionError as exc:
                    console.print(f"[yellow]{market.label}: {exc}[/yellow]")
                    continue
            console.print(
                f"{market.label}: {downloaded} velas nuevas de {source} · {total} guardadas"
            )

    asyncio.run(run())


@data_app.command("list")
def data_list() -> None:
    """Lista los históricos descargados."""
    settings = _load_settings()
    store = BarStore(settings.data_dir / "bars")
    table = Table(title="Históricos descargados")
    for column in ("fuente", "símbolo", "timeframe", "velas", "desde", "hasta"):
        table.add_column(column)
    for info in store.datasets():
        table.add_row(
            info.source,
            info.symbol,
            info.timeframe,
            str(info.rows),
            f"{info.first:%Y-%m-%d %H:%M}",
            f"{info.last:%Y-%m-%d %H:%M}",
        )
    console.print(table)


@app.command()
def backtest(
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="Id de la estrategia.")],
    markets: MarketsOption = [],  # noqa: B006
    timeframe: TimeframeOption = None,
    days: Annotated[int, typer.Option(help="Días hacia atrás desde hoy.")] = 90,
    date_from: Annotated[str | None, typer.Option("--from", help="Inicio AAAA-MM-DD.")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="Fin AAAA-MM-DD.")] = None,
    capital: Annotated[str, typer.Option(help="Capital inicial por mercado.")] = "100",
) -> None:
    """Backtest de una estrategia sobre uno o varios mercados, con tabla comparativa."""
    from invertio.backtest.report import print_comparison, save_reports
    from invertio.backtest.runner import backtest_markets

    settings = _load_settings()
    config = _load_app_config(settings)
    tf = timeframe or config.data.timeframe
    start, end = _window(days, date_from, date_to)
    try:
        initial = Decimal(capital)
        if initial <= 0:
            raise InvalidOperation
    except InvalidOperation:
        console.print(f"[red]Capital no válido: {capital}[/red]")
        raise typer.Exit(code=1) from None

    store = BarStore(settings.data_dir / "bars")
    try:
        reports = asyncio.run(
            backtest_markets(
                config,
                settings.config_dir,
                store,
                strategy,
                _markets(config, markets),
                tf,
                start,
                end,
                initial,
            )
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    print_comparison(console, reports)
    folder = save_reports(settings.data_dir / "reports", strategy, reports)
    console.print(f"Informe guardado en {folder.resolve() / 'report.html'}")


def _load_live_config(settings: Settings, config: AppConfig) -> LiveConfig:
    try:
        return load_live_config(settings.config_dir, config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error en config/live.yaml:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _warn(message: str) -> None:
    console.print(f"[yellow]{message}[/yellow]")


@app.command()
def run(
    timeframe: TimeframeOption = None,
    panel: Annotated[bool, typer.Option(help="Arrancar también el panel web.")] = True,
) -> None:
    """Arranca el motor en vivo (modo de .env; por ahora solo paper). Ctrl+C para parar."""
    from invertio.live.app import run_live

    settings = _load_settings()
    if settings.trading_mode is not TradingMode.PAPER:
        console.print(
            f"[red]El modo {settings.trading_mode.value} llega en la fase 4. "
            "Usa TRADING_MODE=paper.[/red]"
        )
        raise typer.Exit(code=1)
    config = _load_app_config(settings)
    live = _load_live_config(settings, config)
    tf = timeframe or live.timeframe or config.data.timeframe
    console.print(f"Arrancando en modo [bold]paper[/bold] con velas de {tf}. Ctrl+C para parar.")
    if panel:
        console.print(f"Panel: [bold]http://localhost:{settings.panel_port}[/bold]")
    try:
        asyncio.run(run_live(settings, config, live, tf, warn=_warn, panel=panel))
    except KeyboardInterrupt:
        console.print("Detenido.")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


@app.command("panel")
def panel_command() -> None:
    """Panel web en solo lectura (historial, backtests, escáner) sin arrancar el motor."""
    from invertio.api.app import create_app
    from invertio.api.context import ApiContext
    from invertio.api.server import build_server, port_available
    from invertio.persistence.db import create_engine_async, session_factory, upgrade_db

    settings = _load_settings()
    config = _load_app_config(settings)
    live = _load_live_config(settings, config)
    upgrade_db(settings)
    if not port_available(settings.panel_port):
        console.print(
            f"[red]El puerto {settings.panel_port} está ocupado (¿ya corre el motor?)[/red]"
        )
        raise typer.Exit(code=1)

    async def serve() -> None:
        db = create_engine_async(settings)
        try:
            context = ApiContext(
                settings, config, live, session_factory(db), BarStore(settings.data_dir / "bars")
            )
            await build_server(create_app(context), settings.panel_port).serve()
        finally:
            await db.dispose()

    console.print(
        f"Panel (solo lectura): [bold]http://localhost:{settings.panel_port}[/bold] "
        "· Ctrl+C para parar"
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        console.print("Detenido.")


@app.command()
def analyze(
    panel: Annotated[bool, typer.Option(help="Arrancar también el panel web.")] = True,
    telegram: Annotated[bool, typer.Option(help="Enviar avisos al chat configurado.")] = True,
    cycles: Annotated[
        int | None, typer.Option(min=1, help="Parar después de N ciclos (validación).")
    ] = None,
    manual_orders: Annotated[
        bool,
        typer.Option("--manual-orders", help="Habilitar confirmaciones manuales reales (50 €)."),
    ] = False,
    simulate: Annotated[
        bool, typer.Option("--simulate", help="Simular compras y ventas con 50 € ficticios.")
    ] = False,
    compare_strategies: Annotated[
        bool,
        typer.Option("--compare-strategies", help="Comparar cuatro estrategias con 50 € cada una."),
    ] = False,
    all_markets: Annotated[
        bool,
        typer.Option("--all-markets", help="Observar todos los pares EUR con datos autenticados."),
    ] = False,
    execution_trial: Annotated[
        bool,
        typer.Option(
            "--execution-trial", help="Ensayo maker/taker y velas 1/5m: cuatro carteras ficticias."
        ),
    ] = False,
    timeframe_trial: Annotated[
        bool,
        typer.Option(
            "--timeframe-trial",
            help="Las cuatro estrategias con velas de 10 min y 1 h: ocho carteras ficticias.",
        ),
    ] = False,
    lab_trial: Annotated[
        bool,
        typer.Option(
            "--lab-trial",
            help="Estrategias aprobadas en el laboratorio (velas de 4 h), 50 € ficticios.",
        ),
    ] = False,
    learned_trial: Annotated[
        bool,
        typer.Option(
            "--learned-trial",
            help="Aprendizajes: ruptura dinámica 4 h y compras maker frente a inmediatas.",
        ),
    ] = False,
) -> None:
    """Analiza Revolut X; --manual-orders habilita los botones de órdenes reales."""
    from invertio.analysis.app import run_analysis
    from invertio.analysis.config import load_analysis_config

    settings = _load_settings()
    config = _load_app_config(settings)
    try:
        analysis = load_analysis_config(settings.config_dir)
        if all_markets:
            analysis = analysis.model_copy(update={"market_scope": "all_eur"})
        console.print(
            "[bold]Análisis experimental[/bold] · Revolut X · velas 1 min · Ctrl+C para parar"
        )
        if panel:
            console.print(f"Panel: [bold]http://localhost:{settings.panel_port}[/bold]")
        if manual_orders:
            console.print("[yellow]Órdenes reales manuales habilitadas; cada envío exige "
                          "confirmación en el panel. Límite acumulado de compras: 50 €.[/yellow]")
        if simulate:
            console.print("[cyan]Simulación automática con dinero ficticio: "
                          "no envía órdenes a Revolut X.[/cyan]")
        if compare_strategies:
            console.print(
                "[cyan]Cuatro estrategias independientes: 50 € ficticios cada una.[/cyan]"
            )
        if execution_trial:
            console.print(
                "[cyan]Prueba de costes: 4 × 50 € ficticios; compras maker pendientes "
                "o inmediatas, velas 1/5 min y ventas taker.[/cyan]"
            )
        if timeframe_trial:
            console.print(
                "[cyan]Velas de 10 min y 1 h: 8 × 50 € ficticios con las mismas cuatro "
                "estrategias.[/cyan]"
            )
        if learned_trial:
            console.print(
                "[cyan]Aprendizajes: 6 × 50 € ficticios, maker frente a inmediata.[/cyan]"
            )
        if lab_trial:
            console.print(
                "[cyan]Estrategias del laboratorio: velas de 4 h, compras maker, "
                "50 € ficticios por cuenta.[/cyan]"
            )
        asyncio.run(run_analysis(settings, config, analysis, panel=panel, telegram_enabled=telegram,
                                 max_cycles=cycles, manual_orders=manual_orders,
                                 simulate=simulate, compare_strategies=compare_strategies,
                                 execution_trial=execution_trial,
                                 timeframe_trial=timeframe_trial,
                                 lab_trial=lab_trial, learned_trial=learned_trial,
                                 warn=_warn))
    except KeyboardInterrupt:
        console.print("Análisis detenido; el historial se conserva.")
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def scan(
    timeframe: TimeframeOption = None,
    all_markets: Annotated[
        bool, typer.Option("--all", help="Todos los pares en EUR de Revolut X, no solo app.yaml.")
    ] = False,
    top: Annotated[int, typer.Option(help="Cuántos mercados mostrar.")] = 25,
) -> None:
    """Nota actual (0-100) de la estrategia de puntuación en cada mercado, con su desglose."""
    from invertio.core.clock import LiveClock
    from invertio.live.scanner import ScanRow, scan_venues
    from invertio.notify.format import pct, price
    from invertio.strategies import load_strategy
    from invertio.strategies.score import FACTORS, ScoreParams, ScoreStrategy, points_str

    settings = _load_settings()
    config = _load_app_config(settings)
    strategy = load_strategy(ScoreStrategy.id, settings.config_dir)
    assert isinstance(strategy.params, ScoreParams)
    params = strategy.params
    tf = timeframe or config.data.timeframe

    async def collect() -> list[tuple[str, ScanRow]]:
        with console.status("Escaneando…") as status:

            def progress(venue: str, done: int, total: int) -> None:
                status.update(f"Escaneando {venue}… {done}/{total}")

            scans = await scan_venues(
                settings,
                config,
                tf,
                params,
                LiveClock().now(),
                all_markets=all_markets,
                on_progress=progress,
            )
        rows: list[tuple[str, ScanRow]] = []
        for venue_scan in scans:
            if venue_scan.skipped:
                _warn(f"{venue_scan.venue}: {venue_scan.skipped}, se omite")
            rows.extend((venue_scan.venue, row) for row in venue_scan.rows)
        return rows

    rows = asyncio.run(collect())
    rows.sort(key=lambda item: item[1].result.score if item[1].result else -1, reverse=True)
    short = {"tendencia": "tend", "momentum": "mom", "macd": "macd", "rsi": "rsi",
             "volumen": "vol", "ruptura": "rupt"}  # fmt: skip
    entry = points_str(params.entry_score)
    table = Table(title=f"Puntuación actual · velas {tf} · entrada ≥ {entry}")
    for column in ("#", "mercado", "nota", *(short[f] for f in FACTORS), "ATR %",
                   "volatilidad suficiente", "precio", "stop", "objetivo", "señal"):  # fmt: skip
        table.add_column(column, justify="left" if column in ("mercado",) else "right")
    for index, (venue_id, row) in enumerate(rows[:top], start=1):
        r = row.result
        if r is None:
            table.add_row(str(index), f"{venue_id}:{row.symbol}", "—", *[""] * 12, row.note)
            continue
        signal = row.signal(params)
        table.add_row(
            str(index),
            f"{venue_id}:{row.symbol}",
            f"[bold]{points_str(r.score)}[/bold]",
            *(points_str(r.points[f]) for f in FACTORS),
            pct(r.atr_pct, signed=False),
            "sí" if r.tradable else "[yellow]no[/yellow]",
            price(r.close),
            price(r.stop_loss),
            price(r.take_profit) if r.take_profit else "—",
            "[green]cumple[/green]" if signal else "",
        )
    console.print(table)
    console.print(
        "[dim]Nota calculada con reglas fijas (config/strategies/puntuacion.yaml); no es una "
        "recomendación. Compruébala en backtest antes de fiarte de ella.[/dim]"
    )


@lab_app.command("download")
def lab_download(
    days: Annotated[
        int | None, typer.Option(help="Días de historia (por defecto lab.yaml).")
    ] = None,
    full: Annotated[
        bool, typer.Option("--full", help="Volver a pedir toda la ventana, no solo lo nuevo.")
    ] = False,
) -> None:
    """Descarga la historia de todas las criptos en EUR comunes a Revolut X y la fuente."""
    from invertio.lab.config import load_lab_config
    from invertio.lab.data import common_symbols, download_many

    settings = _load_settings()
    lab = load_lab_config(settings.config_dir)
    store = BarStore(settings.data_dir / "bars")

    async def run() -> dict[str, int]:
        venue = _load_app_config(settings).venue(lab.venue)
        symbols = await common_symbols(lab.venue, lab.source, venue.quote_currency)
        with console.status(f"Descargando {len(symbols)} monedas…") as status:
            return await download_many(
                lab.source,
                symbols,
                lab.base_timeframe,
                days or lab.history_days,
                store,
                venue=lab.venue,
                on_symbol=lambda s, i, n: status.update(f"Descargando {s} ({i}/{n})…"),
                full=full,
            )

    totals = asyncio.run(run())
    console.print(f"{len(totals)} monedas con historia de {lab.base_timeframe} guardada.")


@lab_app.command("run")
def lab_run(
    workers: Annotated[
        int | None, typer.Option(help="Procesos en paralelo (por defecto, núcleos − 1).")
    ] = None,
    strategy: Annotated[
        list[str], typer.Option("--strategy", "-s", help="Solo estas estrategias (repetible).")
    ] = [],  # noqa: B006
) -> None:
    """Optimiza cada estrategia en entrenamiento y la juzga en el último año (fuera de muestra)."""
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TimeRemainingColumn

    from invertio.lab.config import load_lab_config
    from invertio.lab.report import print_lab, save_lab
    from invertio.lab.runner import lab_app_config, run_lab
    from invertio.live.feeds import CcxtLiveFeed

    settings = _load_settings()
    config = _load_app_config(settings)
    lab = load_lab_config(settings.config_dir)
    store = BarStore(settings.data_dir / "bars")

    async def market_info() -> tuple[list[str], dict[str, Any], dict[str, float]]:
        feed = CcxtLiveFeed(lab.venue, lab.venue)
        try:
            venue_symbols = await feed.symbols(config.venue(lab.venue).quote_currency)
            symbols = [s for s in venue_symbols if store.info(lab.source, s, lab.base_timeframe)]
            return symbols, await feed.instrument_rules(symbols), await feed.spreads(symbols)
        finally:
            await feed.close()

    with console.status("Leyendo precisión y spreads reales de Revolut X…"):
        symbols, rules, spreads = asyncio.run(market_info())
    if not symbols:
        console.print("[red]No hay historia descargada. Ejecuta: invertio lab download[/red]")
        raise typer.Exit(code=1)
    lab_config = lab_app_config(config, lab.venue, symbols, rules, spreads)

    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Preparando…", total=None)
        result = run_lab(
            lab,
            lab_config,
            settings.data_dir / "bars",
            workers=workers,
            only_strategies=strategy or None,
            spreads=spreads,
            on_phase=lambda text: progress.update(task_id, description=text, completed=0),
            on_progress=lambda done, total: progress.update(task_id, completed=done, total=total),
        )
    print_lab(console, result, lab)
    folder = save_lab(settings.data_dir / "lab", result, lab)
    console.print(f"Informe: {folder.resolve() / 'report.html'}")


@lab_app.command("rotation")
def lab_rotation() -> None:
    """Rotación semanal por momento: una cartera con las monedas más fuertes de cada semana."""
    from invertio.lab.config import load_lab_config
    from invertio.lab.data import resample
    from invertio.lab.report import print_rotation, save_rotation
    from invertio.lab.rotation import daily_series, run_rotation
    from invertio.live.feeds import CcxtLiveFeed

    settings = _load_settings()
    config = _load_app_config(settings)
    lab = load_lab_config(settings.config_dir)
    if lab.rotation is None:
        console.print("[red]Falta la sección rotation en config/lab.yaml[/red]")
        raise typer.Exit(code=1)
    store = BarStore(settings.data_dir / "bars")
    venue = config.venue(lab.venue)

    async def spreads() -> tuple[list[str], dict[str, float]]:
        feed = CcxtLiveFeed(lab.venue, lab.venue)
        try:
            listed = await feed.symbols(venue.quote_currency)
            symbols = [s for s in listed if store.info(lab.source, s, lab.base_timeframe)]
            return symbols, await feed.spreads(symbols)
        finally:
            await feed.close()

    with console.status("Leyendo spreads reales de Revolut X…"):
        symbols, measured = asyncio.run(spreads())
    if "BTC/EUR" not in symbols:
        console.print("[red]Falta la historia de BTC/EUR. Ejecuta: invertio lab download[/red]")
        raise typer.Exit(code=1)
    daily = {
        s: resample(store.read(lab.source, s, lab.base_timeframe, venue=lab.venue), "1d")
        for s in symbols
    }
    # Coste por lado sin comisión: medio spread real (con el mínimo del venue) + deslizamiento.
    costs = {
        s: max(measured.get(s, 0.0), venue.simulation.spread_pct) / 2
        + venue.simulation.slippage_pct
        for s in symbols
    }
    now = datetime.now(UTC)
    result = run_rotation(
        lab.rotation,
        {s: daily_series(bars) for s, bars in daily.items()},
        daily["BTC/EUR"],
        costs=costs,
        test_start=now - timedelta(days=lab.test_days),
        end=now,
    )
    print_rotation(console, result, lab.rotation)
    folder = save_rotation(settings.data_dir / "lab", result, lab.rotation)
    console.print(f"Resultados: {folder.resolve() / 'summary.json'}")


@paper_app.command("report")
def paper_report() -> None:
    """Resultados del paper trading por mercado (cripto vs acciones), netos de costes."""
    from invertio.live.report import MarketSummary, VenueSummary, paper_summary
    from invertio.notify.format import money, pct, signed_money
    from invertio.persistence.db import create_engine_async, session_factory, upgrade_db

    settings = _load_settings()
    config = _load_app_config(settings)
    live = _load_live_config(settings, config)
    upgrade_db(settings)

    async def load() -> tuple[list[VenueSummary], list[MarketSummary]]:
        db = create_engine_async(settings)
        try:
            return await paper_summary(session_factory(db), config, live)
        finally:
            await db.dispose()

    venues, markets = asyncio.run(load())
    table = Table(title="Paper trading: capital por venue")
    for column in ("venue", "desde", "hasta", "inicial", "actual", "retorno", "drawdown máx"):
        table.add_column(column)
    for v in venues:
        table.add_row(
            v.venue,
            f"{v.first:%Y-%m-%d %H:%M}" if v.first else "—",
            f"{v.last:%Y-%m-%d %H:%M}" if v.last else "—",
            money(v.initial, v.currency),
            money(v.equity, v.currency),
            pct(v.return_pct),
            pct(v.max_drawdown_pct, signed=False),
        )
    console.print(table)
    table = Table(title="Paper trading: operaciones cerradas por mercado (netas de costes)")
    for column in ("mercado", "operaciones", "aciertos", "resultado", "comisiones", "abierta"):
        table.add_column(column)
    for m in markets:
        table.add_row(
            f"{m.venue}:{m.symbol}",
            str(m.trades),
            pct(m.win_rate_pct, signed=False),
            signed_money(m.pnl, m.currency),
            money(m.fees, m.currency),
            "sí" if m.open_position else "no",
        )
    console.print(table)


@paper_app.command("reset")
def paper_reset(
    yes: Annotated[bool, typer.Option("--yes", help="Confirma el borrado sin preguntar.")] = False,
) -> None:
    """Borra todo el historial del modo paper (señales, órdenes, fills, equity)."""
    from invertio.persistence.db import create_engine_async, session_factory, upgrade_db
    from invertio.persistence.recorder import reset_mode

    if not yes and not typer.confirm("¿Borrar todo el historial de paper trading?"):
        raise typer.Exit()
    settings = _load_settings()
    upgrade_db(settings)

    async def reset() -> None:
        db = create_engine_async(settings)
        try:
            await reset_mode(session_factory(db), TradingMode.PAPER)
        finally:
            await db.dispose()

    asyncio.run(reset())
    console.print("Historial de paper borrado.")


def _telegram_token(settings: Settings) -> str:
    if settings.telegram_bot_token is None:
        console.print("[red]Falta TELEGRAM_BOT_TOKEN en .env (créalo con @BotFather).[/red]")
        raise typer.Exit(code=1)
    return settings.telegram_bot_token.get_secret_value()


@telegram_app.command("chat-id")
def telegram_chat_id() -> None:
    """Muestra tu chat_id: escribe /start a tu bot en Telegram y ejecuta esto."""
    from invertio.notify.telegram import TelegramClient, TelegramError

    token = _telegram_token(_load_settings())

    async def fetch() -> list[dict[str, Any]]:
        client = TelegramClient(token)
        try:
            return await client.get_updates(None, timeout=0)
        finally:
            await client.close()

    try:
        updates = asyncio.run(fetch())
    except TelegramError as exc:
        console.print(f"[red]Telegram: {exc}[/red]")
        raise typer.Exit(code=1) from None
    chats: dict[int, str] = {}
    for update in updates:
        chat = (update.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or ""
    if not chats:
        console.print("Nadie ha escrito al bot todavía: ábrelo en Telegram, envía /start y repite.")
        return
    for chat_id, name in chats.items():
        console.print(f"chat_id [bold]{chat_id}[/bold] ({name}): pon TELEGRAM_CHAT_ID={chat_id}")


@telegram_app.command("test")
def telegram_test() -> None:
    """Envía un mensaje de prueba a tu chat."""
    from invertio.notify.telegram import TelegramClient, TelegramError

    settings = _load_settings()
    token = _telegram_token(settings)
    chat_id = settings.telegram_chat_id
    if chat_id is None:
        console.print("[red]Falta TELEGRAM_CHAT_ID en .env (usa: invertio telegram chat-id)[/red]")
        raise typer.Exit(code=1)

    async def send() -> None:
        client = TelegramClient(token)
        try:
            await client.send_message(chat_id, "✅ invert.io conectado a Telegram")
        finally:
            await client.close()

    try:
        asyncio.run(send())
    except TelegramError as exc:
        console.print(f"[red]Telegram: {exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print("Mensaje enviado. Revisa Telegram.")


if __name__ == "__main__":
    app()
