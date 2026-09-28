"""Formato de números y mensajes en español (1.234,56)."""

from __future__ import annotations

from decimal import Decimal
from html import escape

CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$"}


def number(value: Decimal | float, decimals: int = 2) -> str:
    text = f"{float(value):,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def price(value: Decimal | float) -> str:
    """Precio con los decimales razonables según su magnitud."""
    magnitude = abs(float(value))
    decimals = 2 if magnitude >= 1 else 4 if magnitude >= 0.01 else 8
    return number(value, decimals)


def currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency, currency)


def money(value: Decimal | float, currency: str) -> str:
    return f"{number(value)} {currency_symbol(currency)}"


def signed_money(value: Decimal | float, currency: str) -> str:
    sign = "+" if float(value) > 0 else "−" if float(value) < 0 else ""
    return f"{sign}{money(abs(float(value)), currency)}"


def pct(value: float, *, signed: bool = True) -> str:
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    return f"{sign}{number(abs(value))} %"


def quantity(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def html(text: str) -> str:
    return escape(text, quote=False)
