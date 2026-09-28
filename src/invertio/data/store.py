"""Almacén de velas en Parquet: data/bars/{fuente}/{símbolo}/{timeframe}.parquet.

La "fuente" es el proveedor de los datos (myokx, revolutx, alpaca), que puede no coincidir
con el venue donde se opera: por ejemplo, históricos de OKX para simular Revolut X.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from invertio.core.models import Bar

_SCHEMA: dict[str, pl.DataType] = {
    "open_time": pl.Datetime("ms", "UTC"),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
}


def _safe(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    source: str
    symbol: str
    timeframe: str
    rows: int
    first: datetime
    last: datetime


class BarStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, source: str, symbol: str, timeframe: str) -> Path:
        return self.root / source / _safe(symbol) / f"{timeframe}.parquet"

    def write(self, source: str, symbol: str, timeframe: str, bars: Sequence[Bar]) -> int:
        """Fusiona `bars` con lo ya guardado (sin duplicados, ordenado). Devuelve el total."""
        path = self.path(source, symbol, timeframe)
        new = pl.DataFrame(
            {
                "open_time": [b.open_time for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            schema=_SCHEMA,
        )
        if path.exists():
            new = pl.concat([pl.read_parquet(path), new])
        merged = new.unique(subset="open_time", keep="last").sort("open_time")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        merged.write_parquet(tmp)
        tmp.replace(path)  # escritura atómica: nunca queda un fichero a medias
        return merged.height

    def read(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        *,
        venue: str,
        start: datetime | None = None,
        end: datetime | None = None,
        last: int | None = None,
    ) -> list[Bar]:
        """Lee velas con `open_time` en [start, end); con `last`, solo las `last` más recientes.
        `venue` es donde se simulará operar."""
        path = self.path(source, symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(
                f"No hay datos de {symbol} {timeframe} de {source}. "
                f"Descárgalos con: invertio data download"
            )
        frame = pl.read_parquet(path)
        if start is not None:
            frame = frame.filter(pl.col("open_time") >= start)
        if end is not None:
            frame = frame.filter(pl.col("open_time") < end)
        if last is not None:
            frame = frame.tail(last)
        return [
            Bar(venue, symbol, timeframe, row[0], row[1], row[2], row[3], row[4], row[5])
            for row in frame.select(list(_SCHEMA)).iter_rows()
        ]

    def info(self, source: str, symbol: str, timeframe: str) -> DatasetInfo | None:
        path = self.path(source, symbol, timeframe)
        return self._info(path, source, symbol, timeframe) if path.exists() else None

    @staticmethod
    def _info(path: Path, source: str, symbol: str, timeframe: str) -> DatasetInfo | None:
        frame = pl.read_parquet(path, columns=["open_time"])
        if frame.height == 0:
            return None
        first, last = frame["open_time"].min(), frame["open_time"].max()
        assert isinstance(first, datetime) and isinstance(last, datetime)
        return DatasetInfo(
            source=source,
            symbol=symbol,
            timeframe=timeframe,
            rows=frame.height,
            first=first.astimezone(UTC),
            last=last.astimezone(UTC),
        )

    def datasets(self) -> list[DatasetInfo]:
        infos = []
        for path in sorted(self.root.glob("*/*/*.parquet")):
            symbol = path.parent.name.replace("-", "/", 1) if "-" in path.parent.name else path.parent.name
            info = self._info(path, path.parent.parent.name, symbol, path.stem)
            if info is not None:
                infos.append(info)
        return infos
