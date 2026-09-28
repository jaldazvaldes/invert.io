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
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(data_app, name="data")
app.add_typer(paper_app, name="paper")
app.add_typer(telegram_app, name="telegram")

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
    tf = timeframe or config.data.timeframe
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
    for column in ("#", "mercado", "nota", *(short[f] for f in FACTORS), "ATR %", "cubre costes",
                   "precio", "stop", "objetivo", "señal"):  # fmt: skip
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
