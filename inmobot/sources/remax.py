"""Fuente: Remax.

Sin API pública: Playwright igual que las demás. A diferencia de
Zonaprop/Argenprop/Mudafy, acá el robots.txt es muy permisivo (solo
desautoriza a AhrefsBot entero y `?associate` para el resto) y sí soporta
paginación real vía `?page=N` (0-indexado) sobre la URL de búsqueda —
verificado navegando manualmente: `?page=1` trae resultados totalmente
distintos a la página base. No hay un tope "duro" impuesto por el sitio
como en las otras fuentes; igual no conviene pasarse de unas pocas páginas
por corrida.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge
from ._text import parse_number, parse_price

log = logging.getLogger(__name__)

BASE = "https://www.remax.com.ar"
MAX_PAGES = 3

CARD_SELECTOR = ".card-remax"

# Angular deja los resultados de la búsqueda serializados en este <script>
# (transfer state) para no volver a pedirlos en el cliente. Ahí viene la
# ubicación de cada aviso, que la tarjeta no muestra.
STATE_JS = "() => document.getElementById('ng-state')?.textContent || ''"

_FEATURE_PATTERNS = [
    (re.compile(r"([\d.,]+)\s*m²\s*totales"), "total_area"),
    (re.compile(r"([\d.,]+)\s*m²\s*cubiertos"), "covered_area"),
    (re.compile(r"(\d+)\s*ambientes"), "rooms"),
    (re.compile(r"(\d+)\s*dormitorios"), "bedrooms"),
    (re.compile(r"(\d+)\s*baños"), "bathrooms"),
]


class RemaxSource:
    name = "remax"

    def __init__(self, conf: dict):
        self.property_slug = conf.get("property_slug", "departamentos")
        self.operation_slug = conf.get("operation_slug", "venta")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.max_pages = int(conf.get("max_pages", MAX_PAGES))
        self.incomplete_zones: set[str] = set()

    def _url(self, zone: str, page: int) -> str:
        base = f"{BASE}/{self.property_slug}-en-{self.operation_slug}-en-{slug(zone)}"
        # page 1 = URL base (sin parámetro); Remax pagina con ?page=N pero
        # 0-indexado, así que nuestra página 2 es su "?page=1".
        return base if page == 1 else f"{base}?page={page - 1}"

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        with browser_page() as page_obj:
            for page_num in range(1, self.max_pages + 1):
                url = self._url(zone, page_num)
                try:
                    page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page_obj.wait_for_selector(CARD_SELECTOR, timeout=10000)
                except Exception as exc:
                    self.incomplete_zones.add(zone)
                    if is_bot_challenge(page_obj):
                        log.warning(
                            "[remax] verificación anti-bot en %s (pág %d) — corto acá, "
                            "no la esquivamos. Quedaron %d página(s).",
                            zone, page_num, page_num - 1,
                        )
                    else:
                        log.info("[remax] corto en %s (pág %d): %s", zone, page_num, exc)
                    return

                cards = page_obj.query_selector_all(CARD_SELECTOR)
                if not cards:
                    return

                coords = coords_by_slug(page_obj.evaluate(STATE_JS))
                for card in cards:
                    item = self._map(card, zone)
                    if item:
                        lat_lon = coords.get(item["source_id"])
                        if lat_lon:
                            item["latitude"], item["longitude"] = lat_lon
                        yield item

                if page_num < self.max_pages:
                    time.sleep(self.delay)

    # ---------------------------------------------------------------- #

    def _map(self, card, zone: str) -> dict | None:
        link_el = card.query_selector('a[href^="/listings/"]')
        href = link_el.get_attribute("href") if link_el else None
        if not href:
            return None
        source_id = href.rsplit("/", 1)[-1]

        price_el = card.query_selector(".card__price")
        expenses_el = card.query_selector(".card__expenses")
        title_el = card.query_selector(".card__description")
        address_el = card.query_selector(".card__address")
        features_el = card.query_selector_all(".card__feature--item")

        price, currency = parse_price(price_el.inner_text() if price_el else None)
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
            "photo_count": len(card.query_selector_all('img[alt^="Foto"]')),
        }
        if address_el:
            item["address"] = address_el.inner_text().strip()
        if expenses_el:
            item["maintenance_fee"] = parse_number(expenses_el.inner_text())

        for feat in features_el:
            text = feat.inner_text()
            for pattern, field in _FEATURE_PATTERNS:
                match = pattern.search(text)
                if match:
                    # Remax escribe los m² a la inglesa ("39.04", "33.420"):
                    # acá el punto es decimal, nunca separador de miles. El
                    # precio y las expensas sí van a la argentina, por eso
                    # esto es solo para las características.
                    item[field] = parse_number(match.group(1), decimal_point=True)
                    break

        return item


def coords_by_slug(state_json: str | None) -> dict[str, tuple[float, float]]:
    """{slug: (lat, lon)} a partir del transfer state de la página.

    Se recorre el JSON entero buscando objetos con `slug` y `location` en vez
    de ir a una ruta fija: la clave de primer nivel es un hash que Angular
    cambia entre builds. Ojo con el orden: es GeoJSON, `[lon, lat]`.
    """
    try:
        stack = [json.loads(state_json or "")]
    except ValueError:
        return {}
    out: dict[str, tuple[float, float]] = {}
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            loc = node.get("location")
            pair = loc.get("coordinates") if isinstance(loc, dict) else None
            if isinstance(node.get("slug"), str) and isinstance(pair, list) and len(pair) == 2:
                lon, lat = pair
                out[node["slug"]] = (float(lat), float(lon))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def build(conf: dict) -> RemaxSource:
    return RemaxSource(conf)
