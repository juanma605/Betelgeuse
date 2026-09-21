"""Fuente: Argenprop.

Sin API pública: Playwright igual que Zonaprop. El robots.txt solo permite
indexar `?pagina-1`, `?pagina-2` y `?pagina-3` (`Disallow: /*?pagina-`
general) — tope duro de 3 páginas por búsqueda.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
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
        # Reordenamientos extra, una sola página cada uno. El robots.txt
        # habilita `Allow: /*?orden-menorprecio` y prohíbe el resto de los
        # órdenes; y con `Disallow: /*?*&*` tampoco se puede combinar un
        # orden con la paginación, así que de cada uno sale una página.
        self.extra_orders = list(conf.get("extra_orders") or [])
        # Cloudflare marca la sesión, no la IP: reusando la misma pestaña
        # corta en la 2da navegación, y abriendo una limpia por página
        # las cinco entran. No se resuelve ni se falsifica nada — se
        # evita que el challenge se dispare. Ver README.
        self.new_session_per_page = bool(conf.get("new_session_per_page", True))
        self.incomplete_zones: set[str] = set()

    def _url(self, property_slug: str, zone: str, page: int, order: str = "") -> str:
        url = f"{BASE}/{property_slug}/{self.operation_slug}/{slug(zone)}"
        if order:
            return url + f"?{order}"
        if page > 1:
            url += f"?pagina-{page}"
        return url

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        with self._sesion() as compartida:
            for i, property_slug in enumerate(self.property_slugs):
                if i > 0:
                    time.sleep(self.delay)
                yield from self._fetch_property_type(compartida, property_slug, zone)

    @contextmanager
    def _sesion(self):
        """Una pestaña para toda la zona, o ninguna si cada página abre la suya."""
        if self.new_session_per_page:
            yield None
        else:
            with browser_page() as page_obj:
                yield page_obj

    def _fetch_property_type(self, page_obj, property_slug: str, zone: str) -> Iterator[dict]:
        yield from self._fetch_pages(page_obj, property_slug, zone, "", self.max_pages)
        # Los más baratos primero: otra lista, no las mismas tarjetas dadas
        # vuelta, y es donde miramos cuando buscamos subvaluados.
        for order in self.extra_orders:
            time.sleep(self.delay)
            yield from self._fetch_pages(page_obj, property_slug, zone, order, 1)

    def _fetch_pages(
        self, page_obj, property_slug: str, zone: str, order: str, max_pages: int
    ) -> Iterator[dict]:
        for page_num in range(1, max_pages + 1):
            url = self._url(property_slug, zone, page_num, order)
            if self.new_session_per_page:
                # La pestaña se cierra antes de ceder los avisos: si el
                # consumidor tarda, no dejamos un Chromium abierto de gusto.
                with browser_page() as propia:
                    avisos, seguir = self._traer_pagina(
                        propia, url, property_slug, zone, page_num
                    )
            else:
                avisos, seguir = self._traer_pagina(
                    page_obj, url, property_slug, zone, page_num
                )

            yield from avisos
            if not seguir:
                return
            if page_num < max_pages:
                time.sleep(self.delay)

        # Salimos del for sin que la lista se vaciara: llegamos al tope de
        # páginas del robots.txt y el inventario sigue. No vimos todo, así
        # que no podemos distinguir "este aviso se vendió" de "este aviso
        # quedó fuera de las páginas que nos dejan mirar", y dar de baja lo
        # segundo mata avisos vivos. Ver el log del 21/09: 815 avisos de
        # Zonaprop dados de baja, y los cuatro que revisé seguían publicados.
        self.incomplete_zones.add(zone)

    def _traer_pagina(
        self, page_obj, url: str, property_slug: str, zone: str, page_num: int
    ) -> tuple[list[dict], bool]:
        """Los avisos de una página, y si tiene sentido pedir la siguiente."""
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
            return [], False

        cards = page_obj.query_selector_all(CARD_SELECTOR)
        if not cards:
            return [], False
        return [i for i in (self._map(c, zone) for c in cards) if i], True

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
