from datetime import datetime
from pathlib import Path

import pytest

from invertio.data.store import BarStore
from tests.helpers import make_bars, minutes


def test_write_merges_deduplicates_and_sorts(tmp_path: Path, t0: datetime) -> None:
    store = BarStore(tmp_path)
    first = make_bars([100, 101, 102], t0)
    second = make_bars([102.5, 103, 104], t0 + minutes(10))  # solapa la 3.ª vela
    assert store.write("myokx", "BTC/EUR", "5m", second) == 3
    assert store.write("myokx", "BTC/EUR", "5m", first) == 5

    bars = store.read("myokx", "BTC/EUR", "5m", venue="revolutx")
    assert [b.open_time for b in bars] == [t0 + minutes(5 * i) for i in range(5)]
    assert bars[2].close == 102  # la última escritura gana en duplicados
    assert all(b.venue == "revolutx" for b in bars)  # venue donde se simula, no la fuente


def test_read_range_and_info(tmp_path: Path, t0: datetime) -> None:
    store = BarStore(tmp_path)
    store.write("myokx", "BTC/EUR", "5m", make_bars([1, 2, 3, 4], t0))
    bars = store.read(
        "myokx", "BTC/EUR", "5m", venue="revolutx", start=t0 + minutes(5), end=t0 + minutes(15)
    )
    assert [b.close for b in bars] == [2, 3]

    info = store.info("myokx", "BTC/EUR", "5m")
    assert info is not None
    assert (info.rows, info.first, info.last) == (4, t0, t0 + minutes(15))
    assert [(d.source, d.symbol, d.timeframe) for d in store.datasets()] == [
        ("myokx", "BTC/EUR", "5m")
    ]


def test_missing_dataset(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    assert store.info("myokx", "BTC/EUR", "5m") is None
    with pytest.raises(FileNotFoundError, match="invertio data download"):
        store.read("myokx", "BTC/EUR", "5m", venue="revolutx")
