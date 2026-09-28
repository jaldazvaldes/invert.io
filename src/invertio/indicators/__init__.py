"""Indicadores incrementales: se actualizan vela a vela con el mismo código en backtest y en vivo.

Cada indicador devuelve `None` hasta tener suficientes datos (`ready`). Las fórmulas siguen
las convenciones de TA-Lib: EMA sembrada con la SMA inicial y suavizado de Wilder en RSI y ATR.
"""

from __future__ import annotations

from collections import deque


class SMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(x)
        self._sum += x
        if len(self._window) == self.period:
            self.value = self._sum / self.period
        return self.value


class EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self._seed = SMA(period)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self.value = self._seed.update(x)
        else:
            self.value += self.alpha * (x - self.value)
        return self.value


class _WilderAverage:
    """Media con suavizado de Wilder: arranca con la media simple de los primeros `period`."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._count = 0
        self._sum = 0.0
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._count += 1
            self._sum += x
            if self._count == self.period:
                self.value = self._sum / self.period
        else:
            self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


class RSI:
    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self._prev: float | None = None
        self._gain = _WilderAverage(period)
        self._loss = _WilderAverage(period)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> float | None:
        if self._prev is not None:
            change = close - self._prev
            avg_gain = self._gain.update(max(change, 0.0))
            avg_loss = self._loss.update(max(-change, 0.0))
            if avg_gain is not None and avg_loss is not None:
                if avg_loss == 0:
                    self.value = 100.0 if avg_gain > 0 else 50.0
                else:
                    self.value = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        self._prev = close
        return self.value


class ATR:
    """Average True Range. El rango verdadero necesita el cierre anterior."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self._prev_close: float | None = None
        self._avg = _WilderAverage(period)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is not None:
            true_range = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
            self.value = self._avg.update(true_range)
        self._prev_close = close
        return self.value


class ROC:
    """Rate of change: variación en % respecto al cierre de hace `period` velas."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self._closes: deque[float] = deque(maxlen=period + 1)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, close: float) -> float | None:
        self._closes.append(close)
        if len(self._closes) == self.period + 1 and self._closes[0] != 0:
            self.value = (close / self._closes[0] - 1) * 100
        return self.value


class MACD:
    """MACD clásico: línea = EMA rápida − EMA lenta; señal = EMA de la línea; histograma."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if not 1 <= fast < slow:
            raise ValueError("MACD necesita 1 <= rápida < lenta")
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._signal = EMA(signal)
        self.line: float | None = None
        self.signal: float | None = None
        self.histogram: float | None = None

    @property
    def ready(self) -> bool:
        return self.histogram is not None

    def update(self, close: float) -> float | None:
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None
        self.line = fast - slow
        self.signal = self._signal.update(self.line)
        if self.signal is not None:
            self.histogram = self.line - self.signal
        return self.histogram


class RollingMax:
    """Máximo de las últimas `period` observaciones."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("El periodo debe ser >= 1")
        self.period = period
        self._window: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, x: float) -> float | None:
        self._window.append(x)
        if len(self._window) == self.period:
            self.value = max(self._window)
        return self.value


__all__ = ["ATR", "EMA", "MACD", "ROC", "RSI", "SMA", "RollingMax"]
