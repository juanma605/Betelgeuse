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
import time
from typing import Iterator

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge
from ._text import parse_number, parse_price

log = logging.getLogger(__name__)

BASE = "https://www.mudafy.com.ar"

# Next.js manda los datos de la página serializados en el HTML (escapados
# dentro de un string de JS). Cada aviso es un objeto `publication` con el
# mismo slug que el href de la tarjeta, y adentro trae sus coordenadas.
_PUBLICATION = re.compile(r'\{"publication":\{"id":\d+,"slug":"([^"]+)"')
_COORDINATES = re.compile(r'"coordinates":\{"latitude":(-?[\d.]+),"longitude":(-?[\d.]+)\}')

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
        raw_slug = conf.get("property_slug", "departamentos")
        self.property_slugs = raw_slug if isinstance(raw_slug, list) else [raw_slug]
        self.operation_slug = conf.get("operation_slug", "venta")
        self.zone_prefix = conf.get("zone_prefix", "caba-")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.incomplete_zones: set[str] = set()

    def _url(self, property_slug: str, zone: str) -> str:
        return (
            f"{BASE}/{self.operation_slug}/{property_slug}/"
            f"{self.zone_prefix}{slug(zone)}"
        )

    @staticmethod
    def _card_selector_for(property_slug: str) -> str:
        # Cada card real tiene un <a> de contenido (con h3, el título) más
        # varios <a> vacíos superpuestos en las fotos del carrusel que
        # apuntan al mismo href — filtramos por ":has(h3)" para quedarnos
        # con uno solo por aviso.
        return f'a[href^="/{property_slug}/"]:has(h3)'

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        with browser_page() as page_obj:
            for i, property_slug in enumerate(self.property_slugs):
                if i > 0:
                    time.sleep(self.delay)
                yield from self._fetch_property_type(page_obj, property_slug, zone)

    def _fetch_property_type(self, page_obj, property_slug: str, zone: str) -> Iterator[dict]:
        url = self._url(property_slug, zone)
        card_selector = self._card_selector_for(property_slug)
        try:
            page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
            page_obj.wait_for_selector(card_selector, timeout=10000)
        except Exception as exc:
            self.incomplete_zones.add(zone)
            if is_bot_challenge(page_obj):
                log.warning(
                    "[mudafy] verificación anti-bot en %s/%s — corto acá, no la esquivamos.",
                    property_slug, zone,
                )
            else:
                log.info("[mudafy] corto en %s/%s: %s", property_slug, zone, exc)
            return

        coords = coords_by_id(page_obj.content())
        cards = page_obj.query_selector_all(card_selector)
        for card in cards:
            item = self._map(card, zone)
            if item:
                lat_lon = coords.get(item["source_id"])
                if lat_lon:
                    item["latitude"], item["longitude"] = lat_lon
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


def coords_by_id(html: str) -> dict[str, tuple[float, float]]:
    """{id del aviso: (lat, lon)} a partir del HTML de la página.

    Las coordenadas de cada aviso se buscan solo entre su `publication` y la
    siguiente, para no asignarle las de un vecino si a alguno le faltan.
    Mudafy ya las publica redondeadas a 3 decimales (~100 m): son aproximadas
    de origen. El `round(…, 6)` solo limpia el ruido de float
    (-34.611000000000004).
    """
    text = html.replace('\\"', '"')
    starts = list(_PUBLICATION.finditer(text))
    out: dict[str, tuple[float, float]] = {}
    for i, pub in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        found = _COORDINATES.search(text, pub.end(), end)
        listing_id = re.search(r"-(\d+)$", pub.group(1))
        if found and listing_id:
            out[listing_id.group(1)] = (
                round(float(found.group(1)), 6),
                round(float(found.group(2)), 6),
            )
    return out


def build(conf: dict) -> MudafySource:
    return MudafySource(conf)
