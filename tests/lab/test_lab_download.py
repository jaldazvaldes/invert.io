from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from invertio.data.store import BarStore
from invertio.lab import data as lab_data
from tests.helpers import make_bars


class _Exchange:
    async def close(self) -> None:
        return None


async def test_download_asks_only_for_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BarStore(tmp_path)
    # Moneda que empezó a cotizar hace poco: su historia empieza mucho después de la ventana.
    first = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=30)
    store.write("myokx", "NEW/EUR", "1h", make_bars([1.0] * 48, first, symbol="NEW/EUR",
                                                   timeframe="1h"))  # fmt: skip
    asked: list[datetime] = []

    async def fake_download(exchange: Any, symbol: str, timeframe: str, since: datetime,
                            until: datetime, **_: Any) -> list[Any]:  # fmt: skip
        asked.append(since)
        return []

    monkeypatch.setattr(lab_data, "create_exchange", lambda _: _Exchange())
    monkeypatch.setattr(lab_data, "download_ohlcv", fake_download)
    await lab_data.download_many("myokx", ["NEW/EUR"], "1h", 1095, store, venue="revolutx")
    assert asked == [first + timedelta(hours=48)]  # justo después de la última guardada
    await lab_data.download_many(
        "myokx", ["NEW/EUR"], "1h", 1095, store, venue="revolutx", full=True
    )
    assert asked[1] < first - timedelta(days=1000)  # toda la ventana otra vez
