"""Fuente: Argenprop.

Sin API pública: Playwright igual que Zonaprop. El robots.txt solo permite
indexar `?pagina-1`, `?pagina-2` y `?pagina-3` (`Disallow: /*?pagina-`
general) — tope duro de 3 páginas por búsqueda.
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

BASE = "https://www.argenprop.com"
MAX_PAGES = 3

CARD_SELECTOR = "a.card"

# Clase del ícono (basico1-icon-<esto>) -> campo de nuestro esquema.
_ICON_MAP = {
    "superficie_cubierta": "covered_area",
    "superficie_total": "total_area",
    "cantidad_ambientes": "rooms",
    "cantidad_dormitorios": "bedrooms",
    "cantidad_banos": "bathrooms",
    "antiguedad": "age_years",
}


class ArgenpropSource:
    name = "argenprop"

    def __init__(self, conf: dict):
        raw_slug = conf.get("property_slug", "departamentos")
        self.property_slugs = raw_slug if isinstance(raw_slug, list) else [raw_slug]
        self.operation_slug = conf.get("operation_slug", "venta")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.max_pages = min(int(conf.get("max_pages", MAX_PAGES)), MAX_PAGES)
        self.incomplete_zones: set[str] = set()

    def _url(self, property_slug: str, zone: str, page: int) -> str:
        url = f"{BASE}/{property_slug}/{self.operation_slug}/{slug(zone)}"
        if page > 1:
            url += f"?pagina-{page}"
        return url

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
                        "[argenprop] Cloudflare pidió verificación en %s/%s (pág %d) — "
                        "corto acá, no la esquivamos. Quedaron %d página(s).",
                        property_slug, zone, page_num, page_num - 1,
                    )
                else:
                    log.info(
                        "[argenprop] corto en %s/%s (pág %d): %s",
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
        source_id = card.get_attribute("data-item-card")
        href = card.get_attribute("href")
        if not source_id or not href:
            return None

        price_el = card.query_selector(".card__price")
        expenses_el = card.query_selector(".card__expenses")
        title_el = card.query_selector(".card__title")
        subtitle_el = card.query_selector(".card__title--primary")
        address_el = card.query_selector(".card__address")
        counter_el = card.query_selector("[data-photo-counter]")

        price, currency = parse_price(price_el.inner_text() if price_el else None)
        neighborhood, city = _parse_subtitle(
            subtitle_el.inner_text() if subtitle_el else None
        )
        if not neighborhood and address_el:
            neighborhood = _neighborhood_from_address(address_el.inner_text())
        title = (title_el.inner_text().strip() if title_el else None) or zone

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
        }

        if address_el:
            item["address"] = address_el.inner_text().strip()

        if expenses_el:
            raw = expenses_el.get_attribute("title") or expenses_el.inner_text()
            item["maintenance_fee"] = parse_number(raw)

        if counter_el:
            match = re.search(r"(\d+)", counter_el.inner_text())
            item["photo_count"] = int(match.group(1)) if match else 1
        else:
            item["photo_count"] = 1 if card.query_selector("img") else 0

        for li in card.query_selector_all(".card__main-features li"):
            icon = li.query_selector("i")
            cls = icon.get_attribute("class") if icon else ""
            field = next((f for key, f in _ICON_MAP.items() if key in (cls or "")), None)
            if field:
                item[field] = parse_number(li.inner_text())

        if item.get("rooms") is None:
            match = re.search(r"(\d+)\s*ambiente", title, re.IGNORECASE)
            if match:
                item["rooms"] = parse_number(match.group(1))

        return item


def _parse_subtitle(text: str | None) -> tuple[str | None, str | None]:
    """"Departamento en Venta en Caballito, CABA" -> ("Caballito", "CABA").

    Usamos el ÚLTIMO " en " antes de la ciudad (no el primero: "Venta" y el
    tipo de propiedad también llevan "en" adelante).
    """
    if not text:
        return None, None
    head, _, city = text.strip().rpartition(",")
    if not head:
        head, city = city, None
    idx = head.lower().rfind(" en ")
    neighborhood = head[idx + 4 :].strip() if idx != -1 else head.strip()
    return neighborhood or None, (city.strip() if city else None)


def _neighborhood_from_address(text: str | None) -> str | None:
    """"Mexico 4200, Piso 3, Almagro Sur" -> "Almagro Sur".

    Respaldo para cuando la tarjeta no trae `.card__title--primary`, que es
    de donde salían barrio y ciudad. La dirección siempre termina en el
    barrio, salvo que venga sin comas — ahí es solo la calle y preferimos
    devolver nada antes que guardar "Bulnes 700" como si fuera un barrio.
    """
    if not text:
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts[-1] if len(parts) > 1 else None


def build(conf: dict) -> ArgenpropSource:
    return ArgenpropSource(conf)
