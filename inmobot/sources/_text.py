"""Parseo de texto compartido entre fuentes basadas en scraping de HTML."""

from __future__ import annotations

import re


_THOUSANDS_ONLY = re.compile(r"^\d{1,3}(\.\d{3})+$")


def parse_number(text: str | None, decimal_point: bool = False) -> float | None:
    """Estilo AR ("1.234,5" -> 1234.5) pero sin asumir que un punto solo
    siempre es separador de miles: "174.37" (Remax, m² con decimales) no
    lleva coma y el punto ahí es decimal. Solo se interpreta como miles
    cuando el patrón es inequívoco (grupos de exactamente 3 dígitos).

    `decimal_point=True` apaga hasta esa interpretación, para campos donde
    sabemos que el punto nunca separa miles. Hace falta porque "33.420" es
    genuinamente ambiguo: son 33.420 m² o 33,42 m² según quién lo escribió, y
    el string solo no alcanza para decidir. Leerlo mal no es un redondeo:
    mete un departamento de 33.420 m² en la base y te arruina el promedio de
    la zona entera.
    """
    if not text:
        return None
    match = re.search(r"[\d.,]+", text)
    if not match:
        return None
    raw = match.group(0)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif not decimal_point and _THOUSANDS_ONLY.match(raw):
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    if not text:
        return None, None
    upper = text.upper()
    if "USD" in upper or "U$S" in upper or "US$" in upper:
        currency = "USD"
    elif "$" in text:
        currency = "ARS"
    else:
        currency = None
    return parse_number(text), currency


_TOTAL = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)")


def parse_total(text: str | None) -> int | None:
    """El total de avisos que declara una búsqueda: "11.972 Departamentos en
    venta en Palermo" -> 11972, "5.815 resultados" -> 5815.

    Es un conteo, nunca lleva decimales: acá el punto siempre separa miles,
    así que no hace falta la cautela de parse_number.
    """
    if not text:
        return None
    match = _TOTAL.search(text)
    return int(match.group(1).replace(".", "")) if match else None
