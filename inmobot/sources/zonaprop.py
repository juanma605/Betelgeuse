"""Fuente: Zonaprop.

Sin API pública: hay que renderizar con Playwright. El robots.txt del sitio
permite indexar `*-pagina-2.html` a `*-pagina-5.html` pero no más allá
(`Disallow: /*pagina-*.html` general, con `Allow` puntual hasta la 5) — lo
tomamos como tope duro de páginas por búsqueda, no como sugerencia.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterator

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge
from ._text import parse_number, parse_price

log = logging.getLogger(__name__)

BASE = "https://www.zonaprop.com.ar"
MAX_PAGES = 5

CARD_SELECTOR = "[data-posting-type]"

_FEATURE_PATTERNS = [
    (re.compile(r"([\d.,]+)\s*m²\s*tot"), "total_area"),
    (re.compile(r"([\d.,]+)\s*m²\s*cub"), "covered_area"),
    (re.compile(r"(\d+)\s*amb"), "rooms"),
    (re.compile(r"(\d+)\s*dorm"), "bedrooms"),
    (re.compile(r"(\d+)\s*baño"), "bathrooms"),
]


class ZonapropSource:
    name = "zonaprop"

    def __init__(self, conf: dict):
        raw_slug = conf.get("property_slug", "departamentos")
        self.property_slugs = raw_slug if isinstance(raw_slug, list) else [raw_slug]
        self.operation_slug = conf.get("operation_slug", "venta")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.max_pages = min(int(conf.get("max_pages", MAX_PAGES)), MAX_PAGES)
        # Zonas donde el fetch se cortó antes de terminar (bloqueo anti-bot,
        # error de red) — el caller no debe dar de baja avisos ahí solo
        # porque no aparecieron en esta corrida incompleta.
        self.incomplete_zones: set[str] = set()

    def _url(self, property_slug: str, zone: str, page: int) -> str:
        base = f"{property_slug}-{self.operation_slug}-{slug(zone)}"
        if page == 1:
            return f"{BASE}/{base}.html"
        return f"{BASE}/{base}-pagina-{page}.html"

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        with browser_page() as page_obj:
            for i, property_slug in enumerate(self.property_slugs):
                if i > 0:
                    time.sleep(self.delay)
                yield from self._fetch_property_type(page_obj, property_slug, zone)

    def _fetch_property_type(self, page_obj, property_slug: str, zone: str) -> Iterator[dict]:
        for page_num in range(1, self.max_pages + 1):
            url = self._url(property_slug, zone, page_num)
            try:
                page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
                page_obj.wait_for_selector(CARD_SELECTOR, timeout=10000)
            except Exception as exc:
                self.incomplete_zones.add(zone)
                if is_bot_challenge(page_obj):
                    log.warning(
                        "[zonaprop] Cloudflare pidió verificación en %s/%s (pág %d) — "
                        "corto acá, no la esquivamos. Quedaron %d página(s).",
                        property_slug, zone, page_num, page_num - 1,
                    )
                else:
                    log.info(
                        "[zonaprop] corto en %s/%s (pág %d): %s",
                        property_slug, zone, page_num, exc,
                    )
                return

            cards = page_obj.query_selector_all(CARD_SELECTOR)
            if not cards:
                return

            for card in cards:
                item = self._map(card, zone)
                if item:
                    yield item

            if page_num < self.max_pages:
                time.sleep(self.delay)

    # ---------------------------------------------------------------- #

    def _map(self, card, zone: str) -> dict | None:
        source_id = card.get_attribute("data-id")
        href = card.get_attribute("data-to-posting")
        if not source_id or not href:
            return None

        price_el = card.query_selector('[data-qa="POSTING_CARD_PRICE"]')
        expenses_el = card.query_selector('[data-qa="expensas"]')
        features_el = card.query_selector('[data-qa="POSTING_CARD_FEATURES"]')
        location_el = card.query_selector('[data-qa="POSTING_CARD_LOCATION"]')
        desc_el = card.query_selector('[data-qa="POSTING_CARD_DESCRIPTION"] a')
        address_el = card.query_selector('[class*="location-address"]')
        has_photo = card.query_selector('[data-qa="POSTING_CARD_GALLERY"] img') is not None

        price, currency = parse_price(price_el.inner_text() if price_el else None)
        neighborhood, city = _parse_location(
            location_el.inner_text() if location_el else None
        )

        # La foto principal es lazy (carga con hover/scroll) y a veces solo
        # queda el ícono "Imagen siguiente" en el momento del scrape — nada
        # confiable como título. Usamos el arranque de la descripción, que
        # siempre está en el HTML inicial.
        title = _short_title(desc_el.inner_text() if desc_el else None) or zone

        item: dict = {
            "id": f"{self.name}:{source_id}",
            "source": self.name,
            "source_id": source_id,
            "url": BASE + href if href.startswith("/") else href,
            "title": title,
            "zone": zone,
            "price": price,
            "currency": currency,
            "neighborhood": neighborhood,
            "city": city,
            # Zonaprop no publica coordenadas, pero sí calle y altura: con eso
            # se ubica después (ver inmobot/geocode.py).
            "address": _direccion(address_el.inner_text() if address_el else None),
            "photo_count": 1 if has_photo else 0,
        }
        item.update(_parse_features(features_el.inner_text() if features_el else ""))
        if expenses_el:
            item["maintenance_fee"] = parse_number(expenses_el.inner_text())
        return item


def _short_title(text: str | None, max_len: int = 120) -> str | None:
    """Primeros ~120 caracteres de la descripción, como sustituto de título
    (Zonaprop no expone un título corto separado en la card de resultados)."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def _direccion(texto: str | None, max_largo: int = 60) -> str | None:
    """Calle y altura de la tarjeta ("Gascon al 300", "Muñiz 778").

    Cuando el aviso no publica dirección, Zonaprop usa ese mismo elemento para
    meter el título entero ("Departamento en Almagro en Venta I 2 Ambientes
    con Balcón, Vestidor y Amenities"). Un título es largo: con el tope de
    caracteres alcanza para distinguirlos, y geocode.limpiar_direccion
    descarta después lo que no tenga altura.
    """
    if not texto:
        return None
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto if len(texto) <= max_largo else None


def _parse_location(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    parts = [p.strip() for p in text.split(",")]
    neighborhood = parts[0] if parts else None
    city = parts[1] if len(parts) > 1 else None
    return neighborhood, city


def _parse_features(text: str) -> dict:
    out: dict = {}
    for pattern, field in _FEATURE_PATTERNS:
        match = pattern.search(text)
        if match:
            out[field] = parse_number(match.group(1))
    return out


def build(conf: dict) -> ZonapropSource:
    return ZonapropSource(conf)
