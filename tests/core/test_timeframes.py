from datetime import UTC, datetime, timedelta

import pytest

from invertio.core.timeframes import floor_to_timeframe, timeframe_delta, timeframe_seconds


@pytest.mark.parametrize(
    ("tf", "seconds"), [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("1d", 86400)]
)
def test_timeframe_seconds(tf: str, seconds: int) -> None:
    assert timeframe_seconds(tf) == seconds
    assert timeframe_delta(tf) == timedelta(seconds=seconds)


@pytest.mark.parametrize("tf", ["", "m", "5", "0m", "5x", "-5m", "1.5h"])
def test_invalid_timeframes(tf: str) -> None:
    with pytest.raises(ValueError):
        timeframe_seconds(tf)


def test_floor_to_timeframe() -> None:
    ts = datetime(2026, 1, 5, 14, 37, 12, tzinfo=UTC)
    assert floor_to_timeframe(ts, "5m") == datetime(2026, 1, 5, 14, 35, tzinfo=UTC)
    assert floor_to_timeframe(ts, "1h") == datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
