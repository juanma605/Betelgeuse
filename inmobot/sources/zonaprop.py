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
from contextlib import contextmanager
from typing import Iterator

import tempfile
from datetime import date
from pathlib import Path

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge
from ._sitemap import descargar_con_navegador, rutas_por_zona_planas, zona_de_barrio
from ._text import parse_number, parse_price, parse_total

log = logging.getLogger(__name__)

BASE = "https://www.zonaprop.com.ar"
MAX_PAGES = 5

CARD_SELECTOR = "[data-posting-type]"

# Zonaprop mezcla en el listado avisos sueltos (PROPERTY) y edificios
# enteros (DEVELOPMENT). Se espera por el atributo a secas para no
# depender de su valor al cargar la página, y se filtra en _map().
TIPO_AVISO_SUELTO = "PROPERTY"

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
        # Reordenamientos extra de la misma búsqueda. El robots.txt tiene
        # `Allow: *-orden-precio-ascendente.html` seguido de
        # `Disallow: *-orden-*`: de todos los órdenes que ofrece el sitio,
        # ese es el único que nos habilitan, y lo pedimos solo en su
        # primera página, que es lo que el Allow cubre al pie de la letra.
        self.extra_orders = list(conf.get("extra_orders") or [])
        # Cloudflare marca la sesión, no la IP: reusando la misma pestaña
        # corta en la 2da navegación, y abriendo una limpia por página
        # las cinco entran. No se resuelve ni se falsifica nada — se
        # evita que el challenge se dispare. Ver README.
        self.new_session_per_page = bool(conf.get("new_session_per_page", True))
        # Zonas donde el fetch se cortó antes de terminar (bloqueo anti-bot,
        # error de red) — el caller no debe dar de baja avisos ahí solo
        # porque no aparecieron en esta corrida incompleta.
        self.incomplete_zones: set[str] = set()
        # Zonas que se leyeron bien pero hasta el tope que permite el
        # robots.txt, con inventario todavía por delante. Distinto de las
        # incompletas: acá el dato que trajimos es bueno, lo que no sabemos
        # es qué hay más allá. Un aviso ausente puede haberse vendido o
        # haber quedado fuera de la ventana, y eso se resuelve con el tiempo
        # (ver storage.max_missed_runs), no en esta corrida.
        self.capped_zones: set[str] = set()
        # Sitemap de listados, declarado en el robots.txt del sitio.
        # Armando la URL a mano se llega a una búsqueda por zona; el
        # sitemap ofrece 325 para las mismas diez, entre sub-barrios
        # (bajo-palermo, botanico-palermo), ambientes y atributos.
        self.sitemap = conf.get("sitemap", "sitemaps_https.xml")
        self.max_searches_per_zone = int(conf.get("max_searches_per_zone", 6))
        self._rutas: dict[str, list[str]] | None = None
        # Lo que la tarjeta pone después de la coma ("Belgrano C, Belgrano",
        # "Palermo, Capital Federal") en las búsquedas propias de cada zona.
        # Es la referencia para validar las rutas del sitemap: desde el slug
        # no se distingue `villa-general-belgrano` (Córdoba) de
        # `barrancas-de-belgrano`, pero la tarjeta de la primera dice
        # "Villa General Belgrano, Córdoba" y Córdoba no es padre de ningún
        # aviso de las búsquedas propias.
        self._padres: set[str] = set()
        # Las zonas del config, para archivar cada aviso en la de su barrio.
        self._zonas: list[str] = []
        # Cuántos avisos dice tener el portal en cada zona (ver
        # db.search_totals). Solo de la búsqueda base de la zona.
        self.totals: dict[str, int] = {}
        # El encabezado de la última página traída ("11.972 Departamentos
        # en venta en Palermo, CABA"): _fetch_pages decide si es el total de
        # la zona, porque solo cuenta el de la búsqueda base.
        self._ultimo_total: int | None = None
        self._ultimo_titulo = ""

    def _ruta_base(self, property_slug: str, zone: str, order: str = "") -> str:
        return f"/{property_slug}-{self.operation_slug}-{slug(zone)}{order}.html"

    def _url(self, ruta: str, page: int) -> str:
        if page == 1:
            return BASE + ruta
        return f"{BASE}{ruta.removesuffix('.html')}-pagina-{page}.html"

    def _paginas_de(self, ruta: str) -> int:
        """Las páginas que el robots.txt permite pedir de esta búsqueda.

        `Allow: *-orden-precio-ascendente.html` habilita esa URL exacta, no
        su paginación, así que de las reordenadas sale una sola página.
        """
        return 1 if "-orden-" in ruta else self.max_pages

    def _rutas_de(self, zone: str, property_slug: str, zonas: list[str]) -> list[str]:
        """Las búsquedas a recorrer para esta zona, según el sitemap."""
        propia = self._ruta_base(property_slug, zone)
        if not self.sitemap:
            return [propia] + [
                self._ruta_base(property_slug, zone, o) for o in self.extra_orders
            ]

        if self._rutas is None:
            self._rutas = self._bajar_sitemap(property_slug, zonas)

        del_sitemap = self._rutas.get(zone) or []
        if del_sitemap and propia not in del_sitemap:
            # Zonaprop no reconoce el slug armado a mano y no da 404:
            # `departamentos-venta-belgrano-r.html` devuelve Belgrano entero
            # (6.670 avisos) y `-canitas.html` un departamento de Córdoba. El
            # sitemap los nombra `belgrano-r-belgrano` y `las-canitas`: la
            # búsqueda base pasa a ser la más corta que el sitemap sí lista,
            # que es la del barrio sin filtros.
            propia = min(del_sitemap, key=len)
        resto = [r for r in del_sitemap if r != propia]
        cupo = self.max_searches_per_zone
        if not cupo or len(resto) + 1 <= cupo:
            return [propia] + resto

        # Rotación: la búsqueda del barrio entero va siempre y el resto se
        # reparte entre corridas, tomando el tramo según el día del año. Es
        # determinístico y en `len(resto)/(cupo-1)` días se recorren todas.
        toman = cupo - 1
        arranque = (date.today().timetuple().tm_yday * toman) % len(resto)
        log.info(
            "[zonaprop] %s: %d búsquedas en el sitemap, tomo %d esta corrida.",
            zone, len(resto) + 1, cupo,
        )
        return [propia] + [resto[(arranque + i) % len(resto)] for i in range(toman)]

    def _bajar_sitemap(self, property_slug: str, zonas: list[str]) -> dict[str, list[str]]:
        carpeta = Path(tempfile.gettempdir()) / "inmobot-sitemaps"
        try:
            with browser_page() as page_obj:
                urls = descargar_con_navegador(f"{BASE}/{self.sitemap}", page_obj, carpeta)
        except Exception as exc:
            log.warning("[zonaprop] no pude leer el sitemap: %s", exc)
            return {}

        rutas = rutas_por_zona_planas(
            urls, zonas, f"/{property_slug}-{self.operation_slug}-", BASE,
            # El sitemap lista URLs que el robots.txt igual no deja pedir.
            prohibidas=("-orden-", "-ubicado-en-"),
            permitidas=("-orden-precio-ascendente.html",),
        )
        log.info(
            "[zonaprop] el sitemap ofrece %d búsquedas para las zonas "
            "configuradas (armando la URL a mano sería una por zona).",
            sum(len(v) for v in rutas.values()),
        )
        return rutas

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        zonas = list(search_cfg.get("zones") or [zone])
        self._zonas = zonas
        subzonas = (search_cfg.get("subzones") or {}).get(zone) or []
        with self._sesion() as compartida:
            for i, property_slug in enumerate(self.property_slugs):
                # La primera ruta es siempre la búsqueda base de la zona (ver
                # _rutas_de), que es la que enseña qué ciudades son válidas.
                # Después van los sub-barrios del config (search.subzones), en
                # su orden y fuera del cupo de rotación: son geográficos y no
                # se pisan, a diferencia de los recortes del sitemap
                # (`-con-balcon`, `-2-habitaciones`), que sí.
                rutas = self._rutas_de(zone, property_slug, zonas)
                de_sub = {self._ruta_de_subzona(property_slug, sub, zone): sub for sub in subzonas}
                rutas = rutas[:1] + list(de_sub) + [r for r in rutas[1:] if r not in de_sub]
                for j, ruta in enumerate(rutas):
                    if i or j:
                        time.sleep(self.delay)
                    yield from self._fetch_pages(
                        compartida, ruta, zone, self._paginas_de(ruta),
                        es_propia=j == 0 or ruta in de_sub,
                        es_base=j == 0,
                        esperado=de_sub.get(ruta),
                    )

    def _ruta_de_subzona(self, property_slug: str, sub: str, zone: str) -> str:
        """La búsqueda de un sub-barrio, con el nombre que usa Zonaprop.

        Para algunos es el nombre solo (`palermo-soho`), para otros va
        seguido del barrio: `belgrano-c` devuelve Belgrano entero y
        `botanico` la Argentina entera, pero el sitemap lista
        `belgrano-c-belgrano` y `botanico-palermo`. Si el sitemap tiene esa
        forma se usa esa; si no, la armada a mano, que igual se valida por
        el título (ver _fetch_pages).
        """
        con_padre = self._ruta_base(property_slug, f"{sub} {zone}")
        if con_padre in ((self._rutas or {}).get(zone) or []):
            return con_padre
        return self._ruta_base(property_slug, sub)

    @contextmanager
    def _sesion(self):
        """Una pestaña para toda la zona, o ninguna si cada página abre la suya."""
        if self.new_session_per_page:
            yield None
        else:
            with browser_page() as page_obj:
                yield page_obj

    def _fetch_pages(
        self, page_obj, ruta: str, zone: str, max_pages: int, es_propia: bool = True,
        es_base: bool | None = None, esperado: str | None = None,
    ) -> Iterator[dict]:
        """Las páginas de una búsqueda.

        `es_base`: el total del encabezado es el de la zona (ver totals).
        `esperado`: el sub-barrio que tiene que decir el título, seguido de
        la zona. Zonaprop no da 404 ante un slug que no reconoce, devuelve
        otra búsqueda: si el título no los nombra, se descarta entera.
        """
        es_base = es_propia if es_base is None else es_base
        for page_num in range(1, max_pages + 1):
            url = self._url(ruta, page_num)
            if self.new_session_per_page:
                # La pestaña se cierra antes de ceder los avisos: si el
                # consumidor tarda, no dejamos un Chromium abierto de gusto.
                with browser_page() as propia:
                    avisos, seguir = self._traer_pagina(propia, url, ruta, zone, page_num)
            else:
                avisos, seguir = self._traer_pagina(page_obj, url, ruta, zone, page_num)

            # El título nombra al barrio padre ("en Palermo Soho, Palermo"): se
            # exigen los dos, así un Botánico de Mendoza no pasa por Palermo.
            if esperado and page_num == 1 and (
                f"-en-{slug(esperado)}-{slug(zone)}-" not in f"-{slug(self._ultimo_titulo)}-"
            ):
                log.info(
                    "[zonaprop] %s no es un barrio de Zonaprop (el título dice %r), lo salteo.",
                    esperado, self._ultimo_titulo,
                )
                return
            if es_base and page_num == 1 and self._ultimo_total is not None:
                # Con varios tipos de propiedad, cada uno tiene su búsqueda
                # base y la zona tiene la suma.
                self.totals[zone] = self.totals.get(zone, 0) + self._ultimo_total
            if es_propia:
                self._padres.update(slug(a["city"]) for a in avisos if a.get("city"))
            else:
                ajenos = [a for a in avisos if slug(a.get("city")) not in self._padres]
                avisos = [a for a in avisos if a not in ajenos]
                if ajenos and not avisos:
                    # La ruta entera es de otro lugar: ni la paginamos.
                    log.warning(
                        "[zonaprop] %s es de otro lugar (%s, %s) — la salteo.",
                        ruta, ajenos[0].get("neighborhood"), ajenos[0].get("city"),
                    )
                    return
                if ajenos:
                    log.info(
                        "[zonaprop] %s: descarto %d aviso(s) de fuera de la zona.",
                        ruta, len(ajenos),
                    )

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
        self._ultimo_total = None
        self._ultimo_titulo = ""
        try:
            page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
            page_obj.wait_for_selector(CARD_SELECTOR, timeout=10000)
        except Exception as exc:
            self.incomplete_zones.add(zone)
            if is_bot_challenge(page_obj):
                log.warning(
                    "[zonaprop] Cloudflare pidió verificación en %s (pág %d) — "
                    "corto acá, no la esquivamos. Quedaron %d página(s).",
                    ruta, page_num, page_num - 1,
                )
            else:
                log.info("[zonaprop] corto en %s (pág %d): %s", ruta, page_num, exc)
            return [], False

        titulo = page_obj.query_selector("h1")
        self._ultimo_titulo = titulo.inner_text().strip() if titulo else ""
        self._ultimo_total = parse_total(self._ultimo_titulo)
        cards = page_obj.query_selector_all(CARD_SELECTOR)
        if not cards:
            return [], False
        return [i for i in (self._map(c, zone) for c in cards) if i], True

    # ---------------------------------------------------------------- #

    def _map(self, card, zone: str) -> dict | None:
        # Un emprendimiento no es un departamento: la tarjeta trae el precio
        # mínimo ("desde USD 148.680", el de la unidad más chica) junto con
        # el rango de superficies ("48 a 148 m² tot."), y el parser se
        # quedaba con el precio de la unidad de 48 m² y los m² de la de 148.
        # Eso daba 1.005 USD/m² en Palermo, donde la mediana ronda los
        # 2.700: un 66% de descuento fabricado por nosotros. Mismo criterio
        # que en mercadolibre._is_project.
        if card.get_attribute("data-posting-type") != TIPO_AVISO_SUELTO:
            return None

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
        # La zona es la del barrio que declara el aviso, si es una del
        # config: la búsqueda de Palermo trae avisos de "Las Cañitas", y
        # esos son de Cañitas. Si no es de ninguna, queda la que se buscó.
        zona = zona_de_barrio(neighborhood, self._zonas or [zone]) or zone

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
            "zone": zona,
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
