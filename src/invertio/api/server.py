"""Servidor HTTP del panel (uvicorn) dentro del bucle asyncio de la app."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Generator

import uvicorn
from fastapi import FastAPI

HOST = "127.0.0.1"  # nunca expuesto a la red: para acceso remoto, túnel SSH o Tailscale


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # Ctrl+C lo gestiona la app (parada ordenada del motor), no uvicorn.
        yield


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def build_server(app: FastAPI, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="warning",
        lifespan="off",
        ws="websockets-sansio",
        timeout_graceful_shutdown=3,  # no esperar indefinidamente a paneles abiertos
    )
    return _Server(config)
