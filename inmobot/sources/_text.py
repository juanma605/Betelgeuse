"""Parseo de texto compartido entre fuentes basadas en scraping de HTML."""

from __future__ import annotations

import re


def parse_number(text: str | None) -> float | None:
    """"1.234,5" (miles con punto, decimales con coma, estilo AR) -> 1234.5."""
    if not text:
        return None
    match = re.search(r"[\d.,]+", text)
    if not match:
        return None
    raw = match.group(0).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    upper = text.upper()
    if "USD" in upper or "U$S" in upper:
        currency = "USD"
    elif "$" in text:
        currency = "ARS"
    else:
        currency = None
    return parse_number(text), currency
