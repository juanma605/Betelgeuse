"""Fuente: MercadoLibre, desde el sitio público de inmuebles.

La API quedó cerrada para apps no certificadas: la búsqueda, los ítems y todo
lo que devuelve avisos contesta 403 PolicyAgent aunque el token sea válido, y
la certificación exige 30 usuarios activos y 300 publicaciones (es para
integradores que le venden software a inmobiliarias). Así que ML se lee como
los otros portales: la página de búsqueda del sitio.

- La página viene armada del servidor: alcanza con una request HTTP, sin
  Playwright.
- El robots.txt permite las búsquedas por barrio pero prohíbe paginar
  (`_Desde_`): una página, ~48 avisos, por zona y corrida.
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
from typing import Iterator

import httpx
from bs4 import BeautifulSoup

from ..normalize import slug
from ._browser import USER_AGENT
from ._text import parse_number, parse_price, parse_total

log = logging.getLogger(__name__)

BASE = "https://inmuebles.mercadolibre.com.ar"

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
        self.incomplete_zones: set[str] = set()
        # Ver el comentario en zonaprop.py: leída hasta el tope del
        # robots.txt, con inventario por delante.
        self.capped_zones: set[str] = set()
        # Cuántos avisos dice tener el portal en cada zona (ver
        # db.search_totals). Solo de la búsqueda base de la zona.
        self.totals: dict[str, int] = {}
        self._last_request = 0.0
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=25.0,
            follow_redirects=True,
        )

    def _url(self, zone: str) -> str:
        parts = [
            self.property_slug, self.operation_slug, self.listing_slug,
            self.region_slug, slug(zone),
        ]
        return f"{BASE}/" + "/".join(p for p in parts if p) + "/"

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        # Una request por zona: el espaciado va entre zonas, no entre páginas.
        wait = self.delay - (time.monotonic() - self._last_request)
        if self._last_request and wait > 0:
            time.sleep(wait)
        try:
            response = self.client.get(self._url(zone))
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("[mercadolibre] no pude leer %s: %s", zone, exc)
            self.incomplete_zones.add(zone)
            return
        finally:
            self._last_request = time.monotonic()

        total = total_declarado(response.text)
        if total is not None:
            self.totals[zone] = total
        items = parse_listing_page(response.text, zone)
        if not items:
            # Una búsqueda por barrio de CABA sin ningún resultado es casi
            # seguro un cambio de HTML o un bloqueo, no un barrio vacío: no
            # hay que dar de baja lo que ya teníamos.
            log.warning("[mercadolibre] 0 avisos en %s — ¿cambió la página?", zone)
            self.incomplete_zones.add(zone)
            return

        # El robots.txt prohíbe paginar (`Disallow: /*_Desde_`), así que de
        # los 5.793 avisos que ML tiene en Palermo vemos 48. Un aviso que no
        # aparece tanto puede haberse vendido como haber quedado fuera de esa
        # única página: se decide con el tiempo.
        self.capped_zones.add(zone)
        yield from items


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
