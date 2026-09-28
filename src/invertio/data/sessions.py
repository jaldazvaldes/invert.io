"""Sesión regular de la bolsa de Nueva York (NYSE/Nasdaq): 9:30–16:00, hora de Nueva York.

Para backtests basta filtrar por hora local: los festivos no tienen velas y en los cierres
anticipados las velas simplemente terminan antes. El horario en vivo (festivos incluidos)
se consulta al bróker en la fase 4.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def in_us_regular_session(open_time: datetime) -> bool:
    local = open_time.astimezone(NEW_YORK)
    return local.weekday() < 5 and REGULAR_OPEN <= local.time() < REGULAR_CLOSE
