"""Normalización, filtrado y deduplicado.

Cada fuente devuelve un dict crudo con su propio vocabulario. Acá lo llevamos
a un esquema común, convertimos monedas y aplicamos los filtros del config.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

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
    """
    zone = slug(item.get("neighborhood") or item.get("zone"))
    rooms = item.get("rooms") or 0

    area = item.get("covered_area") or item.get("total_area") or 0
    area_bucket = round(area / area_tolerance) if area_tolerance else area

    price = item.get("price_norm") or 0
    step = max(price * price_tolerance_pct / 100, 1) if price else 1
    price_bucket = round(price / step) if price else 0

    key = f"{zone}|{rooms}|{area_bucket}|{price_bucket}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #

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

    return True, ""


def normalize(item: dict, search_cfg: dict, dedup_cfg: dict) -> dict:
    """Completa price_norm y fingerprint sobre un aviso ya mapeado."""
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
