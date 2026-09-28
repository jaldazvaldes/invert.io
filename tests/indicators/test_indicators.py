import pytest

from invertio.indicators import ATR, EMA, RSI, SMA

# Serie clásica del ejemplo de RSI de StockCharts (RSI de 14 periodos).
# StockCharts publica 70,53 porque redondea las medias intermedias (0,24 y 0,10). El valor
# exacto: subidas 3,34/14 y bajadas 1,40/14 → RS = 2,3857 → RSI = 70,46 (igual que TA-Lib).
CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]  # fmt: skip
EXPECTED_RSI = [70.46, 66.25, 66.48, 69.35, 66.29, 57.92]


def test_sma() -> None:
    sma = SMA(3)
    assert [sma.update(x) for x in (1, 2, 3, 4, 5)] == [None, None, 2, 3, 4]


def test_ema_is_seeded_with_sma_then_recursive() -> None:
    ema = EMA(3)
    values = [ema.update(x) for x in (1, 2, 3, 4, 5)]
    # alpha = 0.5; semilla = SMA(1,2,3) = 2; luego 2 + 0.5*(4-2) = 3; 3 + 0.5*(5-3) = 4
    assert values == [None, None, 2, 3, 4]


def test_rsi_matches_stockcharts_reference() -> None:
    rsi = RSI(14)
    values = [rsi.update(close) for close in CLOSES]
    assert all(v is None for v in values[:14])
    assert values[14:] == pytest.approx(EXPECTED_RSI, abs=0.01)


def test_rsi_extremes() -> None:
    rising = RSI(3)
    for x in range(1, 10):
        rising.update(float(x))
    assert rising.value == 100.0
    flat = RSI(3)
    for _ in range(10):
        flat.update(5.0)
    assert flat.value == 50.0


def test_atr_wilder() -> None:
    atr = ATR(2)
    # El rango verdadero se calcula desde la 2.ª vela (necesita el cierre anterior):
    assert atr.update(11, 9, 10) is None
    assert atr.update(12, 9, 11) is None  # TR = max(12-9, |12-10|, |9-10|) = 3
    assert atr.update(13, 11, 10) == pytest.approx(2.5)  # TR = max(2, |13-11|, |11-11|) = 2
    # TR = max(10-8, |10-10|, |8-10|) = 2 → Wilder: (2.5*1 + 2)/2 = 2.25
    assert atr.update(10, 8, 9) == pytest.approx(2.25)


@pytest.mark.parametrize("cls", [SMA, EMA, RSI, ATR])
def test_invalid_period(cls: type) -> None:
    with pytest.raises(ValueError):
        cls(0)
