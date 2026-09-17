"""Fuente: MercadoLibre.

Arrancamos por acá porque tiene API pública, JSON estructurado y cero
anti-bot. Todo el resto del pipeline se prueba contra datos reales antes de
pelearse con Playwright.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import httpx

log = logging.getLogger(__name__)

API = "https://api.mercadolibre.com"

# Atributos de ML -> nuestro esquema. Agregá acá si querés más campos.
ATTRIBUTE_MAP = {
    "COVERED_AREA": ("covered_area", float),
    "TOTAL_AREA": ("total_area", float),
    "ROOMS": ("rooms", int),
    "BEDROOMS": ("bedrooms", int),
    "FULL_BATHROOMS": ("bathrooms", int),
    "MAINTENANCE_FEE": ("maintenance_fee", float),
    "PROPERTY_AGE": ("age_years", int),
}


class MercadoLibreSource:
    name = "mercadolibre"

    def __init__(self, conf: dict):
        self.conf = conf
        self.site = conf.get("site", "MLA")
        self.category = conf.get("category")
        self.api_filters = conf.get("api_filters") or {}
        self.page_size = int(conf.get("page_size", 50))
        self.max_results = int(conf.get("max_results_per_zone", 1000))
        self.delay = float(conf.get("rate_limit_seconds", 0.4))

        headers = {"User-Agent": "inmobot/0.1"}
        token = conf.get("access_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(headers=headers, timeout=20.0)

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        """Devuelve avisos mapeados a nuestro esquema para una zona."""
        offset = 0
        while offset < self.max_results:
            params = {
                "q": zone,
                "category": self.category,
                "limit": min(self.page_size, self.max_results - offset),
                "offset": offset,
                **self.api_filters,
            }

            try:
                response = self.client.get(f"{API}/sites/{self.site}/search", params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    log.error(
                        "MercadoLibre pidió autenticación (%s). Generá un token y "
                        "ponelo en sources.mercadolibre.access_token.",
                        exc.response.status_code,
                    )
                else:
                    log.error("Error HTTP %s en zona %s", exc.response.status_code, zone)
                return
            except httpx.HTTPError as exc:
                log.error("Fallo de red en zona %s: %s", zone, exc)
                return

            payload = response.json()
            results = payload.get("results") or []
            if not results:
                return

            for raw in results:
                yield self._map(raw, zone)

            total = payload.get("paging", {}).get("total", 0)
            offset += len(results)
            if offset >= total:
                return
            time.sleep(self.delay)

    # ---------------------------------------------------------------- #

    def _map(self, raw: dict, zone: str) -> dict:
        item: dict = {
            "id": f"{self.name}:{raw.get('id')}",
            "source": self.name,
            "source_id": raw.get("id"),
            "url": raw.get("permalink"),
            "title": raw.get("title"),
            "zone": zone,
            "price": raw.get("price"),
            "currency": raw.get("currency_id"),
            "photo_count": len(raw.get("pictures") or []) or (
                1 if raw.get("thumbnail") else 0
            ),
        }

        location = raw.get("location") or raw.get("seller_address") or {}
        item["neighborhood"] = (location.get("neighborhood") or {}).get("name")
        item["city"] = (location.get("city") or {}).get("name")
        item["latitude"] = _as(location.get("latitude"), float)
        item["longitude"] = _as(location.get("longitude"), float)

        for attr in raw.get("attributes") or []:
            mapping = ATTRIBUTE_MAP.get(attr.get("id"))
            if not mapping:
                continue
            field, caster = mapping
            value = (attr.get("value_struct") or {}).get("number")
            if value is None:
                value = attr.get("value_name")
            item[field] = _as(value, caster)

        return item


def _as(value, caster):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.split()[0].replace(",", ".")
        return caster(float(value))
    except (ValueError, TypeError, IndexError):
        return None


def build(conf: dict) -> MercadoLibreSource:
    return MercadoLibreSource(conf)
