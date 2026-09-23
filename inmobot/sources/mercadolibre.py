"""Fuente: MercadoLibre, desde el sitio público de inmuebles.

La API quedó cerrada para apps no certificadas: la búsqueda, los ítems y todo
lo que devuelve avisos contesta 403 PolicyAgent aunque el token sea válido, y
la certificación exige 30 usuarios activos y 300 publicaciones (es para
integradores que le venden software a inmobiliarias). Así que ML se lee como
los otros portales: la página de búsqueda del sitio.

- La página viene armada del servidor: alcanza con una request HTTP, sin
  Playwright.
- El robots.txt permite las búsquedas por barrio pero prohíbe paginar
  (`_Desde_`): una página, 48 avisos, por búsqueda. Para ver más, cada
  búsqueda grande se parte en otras más chicas (sub-barrio, ambientes,
  antigüedad) hasta que entren en una página: ver `_explorar`. Precio y
  superficie no se usan para partir porque el robots.txt los prohíbe
  (`_PriceRange_`, `_PriceMin_`, `_PriceMax_`, `_TOTAL*AREA_`,
  `_COVERED*AREA_`).
- Se busca dentro de "propiedades-individuales" (segmento del path,
  permitido). Sin ese filtro ML pone los emprendimientos arriba y la primera
  página de Caballito eran 48 proyectos de 48.
- Publica m² cubiertos, que Zonaprop y Mudafy no: es de las pocas fuentes
  que pueden alimentar las medianas limpias.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Iterator

import httpx
from bs4 import BeautifulSoup

from ..normalize import slug
from ._browser import USER_AGENT
from ._sitemap import zona_de_barrio
from ._text import parse_number, parse_price, parse_total

log = logging.getLogger(__name__)

BASE = "https://inmuebles.mercadolibre.com.ar"

# Avisos por página de búsqueda. Una búsqueda que declara más que esto no
# entra entera en la única página que se puede pedir: se parte.
POR_PAGINA = 48

# Un 403 o 429 es ML frenándonos: se corta la fuente entera, como en los
# otros portales. Un 404 o un 500 suelto no.
_FRENO = (403, 429)

_FEATURES = [
    (re.compile(r"(\d+)\s*amb", re.IGNORECASE), "rooms"),
    (re.compile(r"(\d+)\s*dorm", re.IGNORECASE), "bedrooms"),
    (re.compile(r"(\d+)\s*baño", re.IGNORECASE), "bathrooms"),
    (re.compile(r"([\d.,]+)\s*m²\s*cub", re.IGNORECASE), "covered_area"),
    (re.compile(r"([\d.,]+)\s*m²\s*tot", re.IGNORECASE), "total_area"),
]
_BARE_M2 = re.compile(r"([\d.,]+)\s*m²\s*$")


class MercadoLibreSource:
    name = "mercadolibre"

    def __init__(self, conf: dict):
        self.property_slug = conf.get("property_slug", "departamentos")
        self.operation_slug = conf.get("operation_slug", "venta")
        self.listing_slug = conf.get("listing_slug", "propiedades-individuales")
        self.region_slug = conf.get("region_slug", "capital-federal")
        self.delay = float(conf.get("rate_limit_seconds", 8.0))
        # Cómo llama ML a una zona del config, cuando no es igual: la zona
        # "Cañitas" para ML es el barrio "Las Cañitas". Buscando "canitas"
        # ML no da error, hace una búsqueda por texto.
        self.zone_aliases: dict[str, str] = dict(conf.get("zone_aliases") or {})
        # Cómo partir una búsqueda que no entra en una página: primero por
        # ambientes, después por antigüedad (ver _explorar). Vacías, no se
        # parte y queda la zona más sus sub-barrios.
        self.split_rooms: list[str] = list(conf.get("split_rooms") or [])
        self.split_age: list[str] = list(conf.get("split_age") or [])
        # Tope de búsquedas por corrida, entre todas las zonas. 0 = sin tope.
        self.max_busquedas = int(conf.get("max_searches_per_run", 0) or 0)
        self.incomplete_zones: set[str] = set()
        # Ver el comentario en zonaprop.py: leída hasta el tope del
        # robots.txt, con inventario por delante.
        self.capped_zones: set[str] = set()
        # Cuántos avisos dice tener el portal en cada zona (ver
        # db.search_totals). Solo de la búsqueda base de la zona.
        self.totals: dict[str, int] = {}
        self._last_request = 0.0
        self._pedidas = 0
        self._frenado = False
        # Lo que devolvió la búsqueda de cada zona (avisos, total) y cuántas
        # búsquedas le tocan: se arma en la primera llamada, ver _preparar.
        self._bases: dict[str, tuple[list[dict], int]] | None = None
        self._presupuesto: dict[str, int | None] = {}
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=25.0,
            follow_redirects=True,
        )

    def _url(self, zone: str, ambientes: str | None = None, antiguedad: str | None = None) -> str:
        """`/departamentos/venta/propiedades-individuales/[2-ambientes/]capital-federal/palermo/[_PROPERTY*AGE_...]`"""
        parts = [
            self.property_slug, self.operation_slug, self.listing_slug,
            ambientes, self.region_slug, slug(zone),
        ]
        url = f"{BASE}/" + "/".join(p for p in parts if p) + "/"
        return url + f"_PROPERTY*AGE_{antiguedad}" if antiguedad else url

    def _pedir(
        self, nombre: str, ambientes: str | None = None, antiguedad: str | None = None
    ) -> httpx.Response | None:
        """Una búsqueda, respetando el espaciado entre requests."""
        if self._frenado:
            return None
        wait = self.delay - (time.monotonic() - self._last_request)
        if self._last_request and wait > 0:
            time.sleep(wait)
        self._pedidas += 1
        try:
            response = self.client.get(self._url(nombre, ambientes, antiguedad))
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _FRENO:
                self._frenado = True
                log.warning(
                    "[mercadolibre] ML respondió %s: nos está frenando. Corto la "
                    "fuente acá, sin reintentar.", exc.response.status_code,
                )
            else:
                log.error("[mercadolibre] no pude leer %s: %s", self._url(nombre, ambientes, antiguedad), exc)
            return None
        except httpx.HTTPError as exc:
            log.error("[mercadolibre] no pude leer %s: %s", self._url(nombre, ambientes, antiguedad), exc)
            return None
        finally:
            self._last_request = time.monotonic()

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        zonas = list(search_cfg.get("zones") or [zone])
        if self._bases is None:
            self._preparar(zonas)
        base = self._bases.get(zone)
        if base is None:
            self.incomplete_zones.add(zone)
            return

        items = self._explorar(zone, base, search_cfg)
        # La zona es la del barrio que declara el aviso, si es una del
        # config: en la búsqueda de Cañitas aparecen avisos de Palermo.
        for item in items:
            item["zone"] = zona_de_barrio(item.get("neighborhood"), zonas) or zone

        # Un aviso que no aparece tanto puede haberse vendido como haber
        # quedado fuera de las búsquedas de esta corrida: se decide con el
        # tiempo.
        self.capped_zones.add(zone)
        yield from items

    def _preparar(self, zonas: list[str]) -> None:
        """La búsqueda de cada zona, antes que nada.

        Da el total de cada zona, que se guarda y con el que se reparte el
        tope de búsquedas: Palermo (5.810 avisos) necesita más que Belgrano R
        (371). Por eso se piden todas primero y no zona por zona.
        """
        self._bases = {}
        for zone in zonas:
            barrio = self.zone_aliases.get(zone, zone)
            response = self._pedir(barrio)
            if response is None:
                continue
            if not es_la_busqueda(response.text, barrio):
                # Lo que devuelve no es el barrio sino una búsqueda por texto,
                # con otro total y avisos de cualquier lado. No se guarda
                # nada, y la zona no cuenta ausencias hasta que se corrija.
                log.warning(
                    "[mercadolibre] ML no reconoce %r como barrio (hizo una búsqueda "
                    "por texto). Poné su nombre de ML en sources.mercadolibre.zone_aliases.",
                    barrio,
                )
                continue
            items = parse_listing_page(response.text, zone)
            if not items:
                # Una búsqueda por barrio de CABA sin ningún resultado es casi
                # seguro un cambio de HTML o un bloqueo, no un barrio vacío.
                log.warning("[mercadolibre] 0 avisos en %s — ¿cambió la página?", zone)
                continue
            total = total_declarado(response.text) or len(items)
            self.totals[zone] = total
            self._bases[zone] = (items, total)

        # El resto del tope se reparte en proporción al tamaño de cada zona.
        if not self.max_busquedas:
            self._presupuesto = {z: None for z in self._bases}
            return
        resto = max(0, self.max_busquedas - self._pedidas)
        suma = sum(total for _, total in self._bases.values()) or 1
        self._presupuesto = {
            z: resto * total // suma for z, (_, total) in self._bases.items()
        }

    def _explorar(self, zone: str, base: tuple[list[dict], int], search_cfg: dict) -> list[dict]:
        """Parte la zona en búsquedas que entren en una página.

        Una búsqueda de ML muestra 48 avisos de los N que declara, y no se
        puede paginar. Pero sí se puede pedir una búsqueda más chica, que
        muestra otros 48. Se parte en niveles, y solo lo que no entra:

          1. la zona en sus sub-barrios (search.subzones);
          2. cada búsqueda de más de 48 avisos, por ambientes;
          3. cada tramo de ambientes de más de 48, por antigüedad.

        Una búsqueda de 48 o menos se ve entera y ahí se para. Cada una se
        valida: el título tiene que nombrar al barrio (y a los ambientes, si
        los hay), y el total no puede superar al de la búsqueda que se
        partió; si no, ML no aplicó el filtro y se descarta.

        Si el tope de la zona no alcanza para todo un nivel, se toma un tramo
        que rota con el día del año: en unos días se recorre todo. Un aviso
        aguanta varias corridas sin verse antes de darse de baja.
        """
        items, total_zona = base
        vistos = {item["id"] for item in items}
        presupuesto = self._presupuesto.get(zone)
        cuenta = {"sub-barrios": 0, "ambientes": 0, "antigüedad": 0}

        def pedir(nivel, barrio, ambientes=None, antiguedad=None, padre=None) -> int | None:
            nonlocal presupuesto
            if presupuesto is not None:
                if presupuesto <= 0:
                    return None
                presupuesto -= 1
            cuenta[nivel] += 1
            response = self._pedir(barrio, ambientes, antiguedad)
            if response is None:
                # No sabemos qué había ahí: que la zona no cuente ausencias.
                self.incomplete_zones.add(zone)
                return None
            titulo = _titulo(response.text)
            if not es_la_busqueda(response.text, barrio) or (
                ambientes and "ambiente" not in titulo.lower()
            ):
                log.info("[mercadolibre] %s no es una búsqueda de ML (%r), la salteo.",
                         self._url(barrio, ambientes, antiguedad), titulo)
                return None
            total = total_declarado(response.text)
            if padre is not None and total is not None and total > padre:
                return None  # el filtro no se aplicó
            for item in parse_listing_page(response.text, zone):
                if item["id"] not in vistos:
                    vistos.add(item["id"])
                    items.append(item)
            return total

        def tramo(nodos: list) -> list:
            """Los nodos que entran en lo que queda del tope, rotando por día."""
            if presupuesto is None or len(nodos) <= presupuesto:
                return nodos
            if presupuesto <= 0:
                return []
            arranque = (date.today().timetuple().tm_yday * presupuesto) % len(nodos)
            return [nodos[(arranque + i) % len(nodos)] for i in range(presupuesto)]

        barrio_zona = self.zone_aliases.get(zone, zone)
        geo = [(barrio_zona, total_zona)]
        for sub in tramo((search_cfg.get("subzones") or {}).get(zone) or []):
            total = pedir("sub-barrios", sub)
            if total:
                geo.append((sub, total))

        # Cada nivel parte solo lo que no entró en una página.
        partibles = [(b, None, t) for b, t in geo if t > POR_PAGINA]
        if self.split_rooms:
            hijos = [(b, a, t) for b, _, t in partibles for a in self.split_rooms]
            partibles = []
            for b, a, padre in tramo(hijos):
                total = pedir("ambientes", b, a, padre=padre)
                if total and total > POR_PAGINA:
                    partibles.append((b, a, total))
        if self.split_age:
            hijos = [(b, a, e, t) for b, a, t in partibles for e in self.split_age]
            for b, a, e, padre in tramo(hijos):
                pedir("antigüedad", b, a, e, padre=padre)

        log.info(
            "[mercadolibre] %s: %d búsquedas (1 de la zona%s), %d avisos.",
            zone, 1 + sum(cuenta.values()),
            "".join(f", {n} por {k}" for k, n in cuenta.items() if n), len(items),
        )
        return items


def _titulo(html: str) -> str:
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    return match.group(1).strip() if match else ""


def es_la_busqueda(html: str, barrio: str) -> bool:
    """Si la página es la búsqueda de ese barrio y no otra cosa.

    El título de una búsqueda por barrio dice "... en Palermo Soho, Capital
    Federal". Una que ML no reconoce devuelve una búsqueda por texto con
    otro título ("Capital federal canitas").
    """
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    return bool(match) and f"-en-{slug(barrio)}-" in f"-{slug(match.group(1))}-"


def total_declarado(html: str) -> int | None:
    """El "5.815 resultados" del encabezado de la búsqueda."""
    match = re.search(r"quantity-results[^>]*>([^<]+)<", html)
    return parse_total(match.group(1)) if match else None


def parse_listing_page(html: str, zone: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    return [item for card in soup.select(".poly-card") if (item := _map(card, zone))]


def _map(card, zone: str) -> dict | None:
    link = card.select_one("a.poly-component__title")
    href = link.get("href", "") if link else ""
    match = re.search(r"MLA-?(\d+)", href)
    # Las publicidades pasan por un tracker (click1.mercadolibre...) en vez
    # de linkear al aviso.
    if not match or "click1." in href:
        return None
    if _is_project(card):
        return None

    source_id = f"MLA{match.group(1)}"
    price, currency = parse_price(_text(card, ".poly-price__current"))
    ubicacion = _text(card, ".poly-component__location")
    neighborhood, city = _parse_location(ubicacion)

    item: dict = {
        "id": f"mercadolibre:{source_id}",
        "source": "mercadolibre",
        "source_id": source_id,
        "url": href.split("#")[0].split("?")[0],
        "title": link.get_text(" ", strip=True) or zone,
        "zone": zone,
        "price": price,
        "currency": currency,
        "neighborhood": neighborhood,
        "city": city,
        # ML no publica coordenadas, pero sí la calle y la altura: alcanza
        # para ubicarlo después (ver inmobot/geocode.py).
        "address": _direccion(ubicacion),
        "photo_count": len(card.select(".poly-card__portada img")),
    }

    for li in card.select(".poly-attributes_list__item"):
        text = li.get_text(" ", strip=True)
        for pattern, field in _FEATURES:
            found = pattern.search(text)
            if found:
                item[field] = parse_number(found.group(1))
                break
        else:
            bare = _BARE_M2.search(text)
            if bare:
                # "62 m²" sin decir si son cubiertos o totales. Va como total:
                # así queda marcado como área estimada y no entra a las
                # medianas, que son solo de m² cubiertos.
                item.setdefault("total_area", parse_number(bare.group(1)))

    return item


def _is_project(card) -> bool:
    """Emprendimiento: un edificio con varias unidades, precio "Desde" y
    rangos de ambientes y m². No es un departamento que se pueda comparar."""
    pill = card.select_one(".poly-component__pill")
    if pill and "EMPRENDIMIENTO" in pill.get_text().upper():
        return True
    return card.select_one(".poly-price__prefix") is not None


def _text(card, selector: str) -> str | None:
    node = card.select_one(selector)
    return node.get_text(" ", strip=True) if node else None


def _direccion(texto: str | None) -> str | None:
    """"Av Belgrano 3700, Almagro, Capital Federal" -> "Av Belgrano 3700".

    Sin altura no sirve para ubicar el aviso: una calle puede tener treinta
    cuadras.
    """
    primera = (texto or "").split(",")[0].strip()
    return primera if re.search(r"\d", primera) else None


def _parse_location(text: str | None) -> tuple[str | None, str | None]:
    """"Mario Bravo 52, Almagro, Capital Federal" -> ("Almagro", "Capital Federal")."""
    parts = [p.strip() for p in (text or "").split(",") if p.strip()]
    if len(parts) < 2:
        return None, None
    return parts[-2], parts[-1]


def build(conf: dict) -> MercadoLibreSource:
    return MercadoLibreSource(conf)
