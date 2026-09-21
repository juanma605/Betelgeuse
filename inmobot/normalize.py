"""Normalización, filtrado y deduplicado.

Cada fuente devuelve un dict crudo con su propio vocabulario. Acá lo llevamos
a un esquema común, convertimos monedas y aplicamos los filtros del config.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Moneda
# --------------------------------------------------------------------------- #

def to_currency(
    amount: float | None,
    from_currency: str | None,
    to_cur: str,
    fx_rates: dict[str, Any],
) -> float | None:
    """Convierte a la moneda de comparación. Devuelve None si no se puede."""
    if amount is None or not from_currency:
        return None
    if from_currency == to_cur:
        return float(amount)

    rate = fx_rates.get(f"{from_currency}_per_{to_cur}")
    if rate:
        return float(amount) / float(rate)

    inverse = fx_rates.get(f"{to_cur}_per_{from_currency}")
    if inverse:
        return float(amount) * float(inverse)

    return None


# --------------------------------------------------------------------------- #
# Texto / fingerprint
# --------------------------------------------------------------------------- #

def slug(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def fingerprint(item: dict, area_tolerance: float = 2, price_tolerance_pct: float = 5) -> str:
    """Huella difusa para detectar el mismo inmueble publicado por varias
    inmobiliarias. Redondeamos área y precio a "cubetas" del tamaño de la
    tolerancia para que valores cercanos caigan en la misma clave.

    El precio va en escala logarítmica porque la tolerancia es porcentual: un
    paso fijo de 5% sirve para 60.000 y es ridículo para 600.000. Ojo con el
    borde: dos valores dentro de la tolerancia caen casi siempre en la misma
    cubeta, pero si quedan a cada lado de un límite, no. Es una huella para
    juntar *candidatos*, no una prueba.
    """
    zone = slug(item.get("neighborhood") or item.get("zone"))
    rooms = item.get("rooms") or 0

    area = item.get("covered_area") or item.get("total_area") or 0
    area_bucket = round(area / area_tolerance) if area_tolerance else area

    price = item.get("price_norm") or 0
    price_bucket = (
        round(math.log(price) / math.log(1 + price_tolerance_pct / 100))
        if price > 0
        else 0
    )

    key = f"{zone}|{rooms}|{area_bucket}|{price_bucket}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #

# Recuadro de la búsqueda. Un aviso con coordenadas afuera no es un aviso
# raro: es la señal de que el portal entendió otra cosa. Remax, con un slug
# de zona que no reconoce, no devuelve 404 — devuelve otra búsqueda. Así
# entraron 148 avisos de Allen, San Jerónimo y Mar del Plata buscando
# "Cañitas".
BBOX_DEFAULT = {
    "lat_min": -34.71, "lat_max": -34.52,   # CABA
    "lon_min": -58.54, "lon_max": -58.33,
}


def dentro_del_recuadro(lat: float | None, lon: float | None, bbox: dict | None = None) -> bool:
    """Un aviso sin coordenadas cuenta como adentro: el filtro descarta lo
    que está probadamente afuera, no lo que no sabemos ubicar."""
    if lat is None or lon is None:
        return True
    b = bbox or BBOX_DEFAULT
    return b["lat_min"] <= lat <= b["lat_max"] and b["lon_min"] <= lon <= b["lon_max"]


# (clave del config, campo del aviso, comparador)
_RULES = [
    ("covered_area_min", "covered_area", "min"),
    ("covered_area_max", "covered_area", "max"),
    ("total_area_min", "total_area", "min"),
    ("total_area_max", "total_area", "max"),
    ("rooms_min", "rooms", "min"),
    ("rooms_max", "rooms", "max"),
    ("bedrooms_min", "bedrooms", "min"),
    ("bedrooms_max", "bedrooms", "max"),
    ("max_maintenance_fee", "maintenance_fee", "max"),
    ("max_age_years", "age_years", "max"),
]


def passes_filters(item: dict, search_cfg: dict) -> tuple[bool, str]:
    """Aplica los filtros del config. Devuelve (pasa, motivo_del_rechazo)."""
    filters = search_cfg.get("filters") or {}

    price = item.get("price_norm")
    pmin, pmax = search_cfg.get("price_min"), search_cfg.get("price_max")
    if price is None:
        return False, "sin precio normalizable"
    if pmin is not None and price < pmin:
        return False, "precio por debajo del rango"
    if pmax is not None and price > pmax:
        return False, "precio por encima del rango"

    for cfg_key, field, mode in _RULES:
        limit = filters.get(cfg_key)
        if limit is None:
            continue
        value = item.get(field)
        if value is None:
            continue  # dato ausente no descalifica: mejor revisarlo a mano
        if mode == "min" and value < limit:
            return False, f"{field} menor al mínimo"
        if mode == "max" and value > limit:
            return False, f"{field} mayor al máximo"

    if filters.get("require_photos") and not (item.get("photo_count") or 0):
        return False, "sin fotos"

    if not dentro_del_recuadro(
        item.get("latitude"), item.get("longitude"), search_cfg.get("bbox")
    ):
        return False, "coordenadas fuera del recuadro de búsqueda"

    return True, ""


def drop_implausible_areas(item: dict, max_area: float | None) -> None:
    """Borra superficies que no pueden ser reales para la búsqueda.

    Hay errores de carga en origen que ningún parser arregla: Remax tiene
    guardado un 1½ ambiente con 33.420 m² cubiertos en su propia base, no es
    un problema de cómo leemos el número. Un solo dato así mete un precio/m²
    de 3 USD y arruina cualquier promedio que toque. Se borra el dato y no el
    aviso, igual que si el portal no lo hubiera publicado.
    """
    if not max_area:
        return
    for field in ("covered_area", "total_area"):
        value = item.get(field)
        if value is not None and value > max_area:
            log.warning(
                "[%s] %s de %s m² descartado por imposible (máx %s): %s",
                item.get("source"), field, value, max_area, item.get("url"),
            )
            item[field] = None


def normalize(item: dict, search_cfg: dict, dedup_cfg: dict) -> dict:
    """Completa price_norm y fingerprint sobre un aviso ya mapeado."""
    drop_implausible_areas(item, search_cfg.get("max_plausible_area_m2"))
    item["price_norm"] = to_currency(
        item.get("price"),
        item.get("currency"),
        search_cfg.get("currency", "USD"),
        search_cfg.get("fx_rates") or {},
    )
    item["fingerprint"] = fingerprint(
        item,
        dedup_cfg.get("area_tolerance_m2", 2),
        dedup_cfg.get("price_tolerance_pct", 5),
    )
    return item
