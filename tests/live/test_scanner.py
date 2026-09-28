import math
from datetime import datetime, timedelta

from invertio.core.models import Bar
from invertio.live.scanner import scan
from invertio.strategies.score import ScoreParams
from tests.helpers import make_bars

PARAMS = ScoreParams(
    ema_fast=5, ema_trend=20, roc_bars=3, breakout_bars=10, volume_period=5, min_atr_pct=0
)


class FakeFeed:
    has_live_spread = False
    venue = "revolutx"

    def __init__(self, t0: datetime) -> None:
        up = [100 * (1.003**i) + math.sin(i) * 0.05 for i in range(120)]
        down = [100 * (0.997**i) + math.sin(i) * 0.05 for i in range(120)]
        self.bars = {
            "UP/EUR": make_bars(up, t0, symbol="UP/EUR"),
            "DOWN/EUR": make_bars(down, t0, symbol="DOWN/EUR"),
            "NEW/EUR": make_bars([1.0, 1.1, 1.2], t0, symbol="NEW/EUR"),
        }

    async def closed_bars(
        self, symbol: str, timeframe: str, since: datetime, now: datetime
    ) -> list[Bar]:
        if symbol == "BROKEN/EUR":
            raise ConnectionError("sin red")
        return [b for b in self.bars[symbol] if since <= b.open_time and b.close_time <= now]

    async def spreads(self, symbols: list[str]) -> dict[str, float]:
        return {}

    async def close(self) -> None:
        pass


async def test_scan_ranks_by_score_and_explains_missing_rows(t0: datetime) -> None:
    feed = FakeFeed(t0)
    now = t0 + timedelta(minutes=5 * 120)
    rows = await scan(feed, ["DOWN/EUR", "NEW/EUR", "UP/EUR", "BROKEN/EUR"], "5m", PARAMS, now)
    assert [r.symbol for r in rows[:2]] == ["UP/EUR", "DOWN/EUR"]
    up, down = rows[0].result, rows[1].result
    assert up is not None and down is not None and up.score > down.score
    assert rows[0].signal(PARAMS) is (up.score >= PARAMS.entry_score)
    notes = {r.symbol: r.note for r in rows[2:]}
    assert "historial insuficiente" in notes["NEW/EUR"]
    assert "error" in notes["BROKEN/EUR"]
