"""Configuración de logs estructurados (structlog)."""

from __future__ import annotations

import io
import logging
import sys

import structlog


def force_utf8_output() -> None:
    """En Windows, si la salida no es una consola (servicio, log redirigido), Python usa
    cp1252 y un emoji en un aviso tumbaría el proceso. Se fuerza UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.encoding.lower() not in (
            "utf-8",
            "utf8",
        ):
            stream.reconfigure(encoding="utf-8", errors="replace")


def configure_logging(level: str = "INFO", *, json: bool = False) -> None:
    force_utf8_output()
    # httpx registra cada URL a nivel INFO; la de Telegram lleva el token del bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Las trazas nunca muestran variables locales: podrían contener claves o tokens.
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(
            exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False)
        )
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            *([structlog.processors.format_exc_info] if json else []),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )
