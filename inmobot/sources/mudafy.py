"""Fuente: Mudafy.

Sin API pública: Playwright igual que Zonaprop/Argenprop. A diferencia de
esas dos, acá no hay paginación crawleable: el listado es un mapa + lista
que carga ~25 avisos de una sola vez, sin botón "cargar más" ni links de
página, y el robots.txt prohíbe cualquier URL con query string
(`Disallow: /*?`) — así que no hay forma respetuosa de pedir más resultados
por búsqueda. Aceptamos ese tope (~25 por zona por corrida) en vez de
inventar una paginación que el sitio no ofrece.

La ficha individual (`/ficha/...`) está explícitamente disallowed, pero no
hace falta: cada card del listado ya trae precio, m², ambientes,
dormitorios, baños, cochera y expensas.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge
from ._text import parse_number, parse_price

log = logging.getLogger(__name__)

BASE = "https://www.mudafy.com.ar"

# Ícono (lucide-<esto>) -> campo de nuestro esquema. "toilet" y "car" no
# tienen equivalente en el esquema, se ignoran.
_ICON_MAP = {
    "lucide-maximize2": "total_area",
    "lucide-layout-grid": "rooms",
    "lucide-bed-double": "bedrooms",
    "lucide-bath": "bathrooms",
}


class MudafySource:
    name = "mudafy"

    def __init__(self, conf: dict):
        self.property_slug = conf.get("property_slug", "departamentos")
        self.operation_slug = conf.get("operation_slug", "venta")
        self.zone_prefix = conf.get("zone_prefix", "caba-")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.incomplete_zones: set[str] = set()

    def _url(self, zone: str) -> str:
        return (
            f"{BASE}/{self.operation_slug}/{self.property_slug}/"
            f"{self.zone_prefix}{slug(zone)}"
        )

    @property
    def _card_selector(self) -> str:
        # Cada card real tiene un <a> de contenido (con h3, el título) más
        # varios <a> vacíos superpuestos en las fotos del carrusel que
        # apuntan al mismo href — filtramos por ":has(h3)" para quedarnos
        # con uno solo por aviso.
        return f'a[href^="/{self.property_slug}/"]:has(h3)'

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        url = self._url(zone)
        with browser_page() as page_obj:
            try:
                page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
                page_obj.wait_for_selector(self._card_selector, timeout=10000)
            except Exception as exc:
                self.incomplete_zones.add(zone)
                if is_bot_challenge(page_obj):
                    log.warning(
                        "[mudafy] verificación anti-bot en %s — corto acá, no la esquivamos.",
                        zone,
                    )
                else:
                    log.info("[mudafy] corto en %s: %s", zone, exc)
                return

            cards = page_obj.query_selector_all(self._card_selector)
            for card in cards:
                item = self._map(card, zone)
                if item:
                    yield item

    # ---------------------------------------------------------------- #

    def _map(self, card, zone: str) -> dict | None:
        href = card.get_attribute("href")
        if not href:
            return None
        match = re.search(r"-(\d+)$", href)
        source_id = match.group(1) if match else href

        title_el = card.query_selector("h3")
        subtitle_el = card.query_selector("h2")
        title = (title_el.inner_text().strip() if title_el else None) or zone
        neighborhood, city = _parse_subtitle(
            subtitle_el.inner_text() if subtitle_el else None
        )

        item: dict = {
            "id": f"{self.name}:{source_id}",
            "source": self.name,
            "source_id": source_id,
            "url": BASE + href if href.startswith("/") else href,
            "title": title,
            "zone": zone,
            "neighborhood": neighborhood,
            "city": city,
            # Las fotos viven en el carrusel, hermano del <a> de contenido
            # (no descendiente) — por eso se busca desde el padre.
            "photo_count": 1 if card.evaluate("el => !!el.parentElement.querySelector('img')") else 0,
        }

        for span in card.query_selector_all("span"):
            icon = span.query_selector("svg")
            cls = icon.get_attribute("class") if icon else ""
            field = next((f for key, f in _ICON_MAP.items() if key in (cls or "")), None)
            if field:
                item[field] = parse_number(span.inner_text())

        for p in card.query_selector_all("div p"):
            text = p.inner_text()
            if "expensas" in text.lower():
                item["maintenance_fee"] = parse_number(text)
            elif "m²" in text or "USD" in text.upper() or "$" in text:
                price, currency = parse_price(text.split("·")[0])
                item["price"], item["currency"] = price, currency

        return item


def _parse_subtitle(text: str | None) -> tuple[str | None, str | None]:
    """"Departamento en venta · Caballito, CABA" -> ("Caballito", "CABA")."""
    if not text:
        return None, None
    _, _, tail = text.partition("·")
    parts = [p.strip() for p in (tail or text).split(",")]
    neighborhood = parts[0] if parts and parts[0] else None
    city = parts[1] if len(parts) > 1 else None
    return neighborhood, city


def build(conf: dict) -> MudafySource:
    return MudafySource(conf)
