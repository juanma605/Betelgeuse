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

from datetime import date

import httpx

from ..normalize import slug
from ._browser import USER_AGENT, browser_page, is_bot_challenge
from ._sitemap import descargar as descargar_sitemap
from ._sitemap import rutas_por_zona
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
        # Ver el comentario en zonaprop.py: leída hasta el tope del
        # robots.txt, con inventario por delante.
        self.capped_zones: set[str] = set()
        # Sitemap de listados que publica el propio robots.txt del sitio.
        # Sin esto solo se llega a `/departamentos/venta/palermo`, que con
        # el tope de 3 páginas da 60 avisos de los ~10.000 que Argenprop
        # tiene ahí. El sitemap descubre además palermo-chico, -hollywood,
        # -soho, -nuevo y -viejo, cada una con sus propias 3 páginas.
        self.sitemap = conf.get("sitemap", "sitemap-listing-venta-caba")
        self._rutas: dict[str, list[str]] | None = None
        # Cuántas de esas búsquedas pedir por corrida. Las 18 de Palermo
        # son 54 páginas seguidas y Cloudflare corta mucho antes: en la
        # prueba del 21/09 pasaron 8. En vez de insistir, se rota — cada
        # corrida toma un tramo distinto y en unos días se recorren
        # todas, que es lo mismo pero sin castigar al portal.
        self.max_searches_per_zone = int(conf.get("max_searches_per_zone", 6))

    def _ruta_base(self, property_slug: str, zone: str) -> str:
        return f"/{property_slug}/{self.operation_slug}/{slug(zone)}"

    def _url(self, ruta: str, page: int, order: str = "") -> str:
        url = BASE + ruta
        if order:
            # `Disallow: /*?*&*`: un orden no se puede combinar con la
            # paginación, así que de esta pasada sale una sola página.
            return f"{url}?{order}"
        if page > 1:
            url += f"?pagina-{page}"
        return url

    def _rutas_de(self, zone: str, property_slug: str, zonas: list[str]) -> list[str]:
        """Las búsquedas a recorrer para esta zona, según el sitemap.

        Si el sitemap no está configurado o no se pudo leer, queda la de
        siempre: el barrio a secas.
        """
        propia = self._ruta_base(property_slug, zone)
        if not self.sitemap:
            return [propia]

        if self._rutas is None:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True
            ) as client:
                urls = descargar_sitemap(f"{BASE}/sitemaps/{self.sitemap}.xml.gz", client)
            self._rutas = rutas_por_zona(
                urls, zonas, property_slug, self.operation_slug, BASE
            )
            total = sum(len(v) for v in self._rutas.values())
            log.info(
                "[argenprop] el sitemap ofrece %d búsquedas para las zonas "
                "configuradas (armando la URL a mano serían %d).",
                total, len(zonas),
            )

        # La propia primero: es la más amplia y la que ya veníamos usando.
        resto = [r for r in (self._rutas.get(zone) or []) if r != propia]
        cupo = self.max_searches_per_zone
        if not cupo or len(resto) + 1 <= cupo:
            return [propia] + resto

        # Rotación: la búsqueda del barrio entero va siempre, y el resto se
        # reparte entre corridas. El tramo se elige por el día del año, así
        # que es determinístico (dos corridas del mismo día piden lo mismo)
        # y en `len(resto) / (cupo - 1)` días se recorren todas.
        toman = cupo - 1
        arranque = (date.today().timetuple().tm_yday * toman) % len(resto)
        elegidas = [resto[(arranque + i) % len(resto)] for i in range(toman)]
        log.info(
            "[argenprop] %s: %d búsquedas en el sitemap, tomo %d esta corrida "
            "(las demás entran en las que siguen).",
            zone, len(resto) + 1, cupo,
        )
        return [propia] + elegidas

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        zonas = list(search_cfg.get("zones") or [zone])
        with self._sesion() as compartida:
            for i, property_slug in enumerate(self.property_slugs):
                if i > 0:
                    time.sleep(self.delay)
                for j, ruta in enumerate(self._rutas_de(zone, property_slug, zonas)):
                    if i or j:
                        time.sleep(self.delay)
                    yield from self._fetch_property_type(compartida, ruta, zone)

    @contextmanager
    def _sesion(self):
        """Una pestaña para toda la zona, o ninguna si cada página abre la suya."""
        if self.new_session_per_page:
            yield None
        else:
            with browser_page() as page_obj:
                yield page_obj

    def _fetch_property_type(self, page_obj, ruta: str, zone: str) -> Iterator[dict]:
        yield from self._fetch_pages(page_obj, ruta, zone, "", self.max_pages)
        # Los más baratos primero: otra lista, no las mismas tarjetas dadas
        # vuelta, y es donde miramos cuando buscamos subvaluados.
        for order in self.extra_orders:
            time.sleep(self.delay)
            yield from self._fetch_pages(page_obj, ruta, zone, order, 1)

    def _fetch_pages(
        self, page_obj, ruta: str, zone: str, order: str, max_pages: int
    ) -> Iterator[dict]:
        for page_num in range(1, max_pages + 1):
            url = self._url(ruta, page_num, order)
            if self.new_session_per_page:
                # La pestaña se cierra antes de ceder los avisos: si el
                # consumidor tarda, no dejamos un Chromium abierto de gusto.
                with browser_page() as propia:
                    avisos, seguir = self._traer_pagina(propia, url, ruta, zone, page_num)
            else:
                avisos, seguir = self._traer_pagina(page_obj, url, ruta, zone, page_num)

            yield from avisos
            if not seguir:
                return
            if page_num < max_pages:
                time.sleep(self.delay)

        # Salimos del for sin que la lista se vaciara: llegamos al tope de
        # páginas del robots.txt y el inventario sigue. Lo que trajimos es
        # bueno; lo que no sabemos es qué hay más allá, así que un aviso
        # ausente tanto puede haberse vendido como haber quedado fuera de la
        # ventana. Se decide con el tiempo, no acá.
        self.capped_zones.add(zone)

    def _traer_pagina(
        self, page_obj, url: str, ruta: str, zone: str, page_num: int
    ) -> tuple[list[dict], bool]:
        """Los avisos de una página, y si tiene sentido pedir la siguiente."""
        try:
            page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
            page_obj.wait_for_selector(CARD_SELECTOR, timeout=10000)
        except Exception as exc:
            self.incomplete_zones.add(zone)
            if is_bot_challenge(page_obj):
                log.warning(
                    "[argenprop] Cloudflare pidió verificación en %s (pág %d) — "
                    "corto acá, no la esquivamos. Quedaron %d página(s).",
                    ruta, page_num, page_num - 1,
                )
            else:
                log.info("[argenprop] corto en %s (pág %d): %s", ruta, page_num, exc)
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
