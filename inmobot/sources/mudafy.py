"""Fuente: Mudafy.

El listado de Mudafy no pagina (mapa + lista de una sola carga, ~25 avisos
por zona) y su robots.txt prohíbe cualquier URL con query string, así que
por las búsquedas no hay manera de ver más de eso. Pero el mismo robots.txt
declara `sitemap_listings.xml`: la lista de los avisos individuales del
sitio, y las fichas (`/departamentos/<dirección>-departamento-en-venta-<id>`)
no están prohibidas — lo prohibido es `/ficha/`, otra ruta.

Entonces se trabaja por fichas:

1. Se juntan candidatos del sitemap y de las ~25 tarjetas de cada zona. Se
   necesitan los dos: medido el 22/09, 20 de los 191 avisos que teníamos
   activos no estaban en el sitemap, y 7 de 8 revisados seguían publicados.
   Y el sitemap está lejos de ser el inventario: lista ~1.370 departamentos
   en venta en todo el país, mientras el listado declara 1.195 solo en
   Palermo (23/09). Esos totales se guardan por corrida (`totals`), aunque
   no se sabe qué cuentan: Colegiales declara 1.123 y Remax tiene 258 ahí.
2. Se pide la ficha de cada candidato que no conocemos, y después se
   refrescan las conocidas de nuestras zonas, de la más vieja a la más nueva,
   hasta `max_fichas_per_run`.
3. De cada ficha sale el aviso entero, incluido el barrio. El sitemap es
   nacional y la URL no dice dónde queda: recién leyendo la ficha se sabe si
   es de Palermo o de Pilar. Eso se anota en un caché (`fichas_cache`) para
   no volver a pedir nunca las ~1.000 fichas que no son de nuestras zonas.

Todo va por HTTP simple: ni el sitemap, ni el listado, ni las fichas pasan
por Cloudflare, así que no hace falta Playwright.

Las tarjetas se usan solo para descubrir avisos, no como datos. La ficha
trae superficie cubierta, antigüedad y expensas que la tarjeta no tiene: si
un aviso llegara unas corridas por tarjeta y otras por ficha, la tarjeta le
pisaría esos campos con null cada vez.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from ..normalize import slug
from ._browser import USER_AGENT
from ._sitemap import descargar, zona_de_barrio
from ._text import parse_total

log = logging.getLogger(__name__)

BASE = "https://mudafy.com.ar"

_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
_ID = re.compile(r"-(\d+)$")

# Cortes seguidos (403, 429, 5xx, red) antes de abandonar la pasada. Un 404
# no cuenta: es un aviso dado de baja, no el sitio frenándonos.
_MAX_FALLAS_SEGUIDAS = 3


class MudafySource:
    name = "mudafy"

    def __init__(self, conf: dict):
        raw_slug = conf.get("property_slug", "departamentos")
        self.property_slugs = raw_slug if isinstance(raw_slug, list) else [raw_slug]
        self.operation_slug = conf.get("operation_slug", "venta")
        self.zone_prefix = conf.get("zone_prefix", "caba-")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.sitemap = conf.get("sitemap", "sitemap_listings.xml")
        self.max_fichas = int(conf.get("max_fichas_per_run", 600))
        # Uno por operación: la corrida de alquileres usa el mismo bloque de
        # config y no puede pisar el caché de ventas.
        self.cache_path = Path(
            conf.get("fichas_cache") or f"data/mudafy_fichas_{self.operation_slug}.json"
        )
        self.incomplete_zones: set[str] = set()
        # Ni las tarjetas ni el sitemap ven la zona entera (ver arriba), así
        # que la ausencia de un aviso no prueba que se vendió: baja paciente.
        self.capped_zones: set[str] = set()
        # Cuántos avisos dice tener el portal en cada zona (ver
        # db.search_totals). Solo de la búsqueda base de la zona.
        self.totals: dict[str, int] = {}
        self._por_zona: dict[str, list[dict]] | None = None
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        # La pasada de fichas es una sola para todas las zonas (el sitemap
        # no viene por zona): se hace en la primera llamada y cada zona
        # después se lleva lo suyo.
        if self._por_zona is None:
            self._por_zona = self._pasada(search_cfg["zones"])
        yield from self._por_zona.pop(zone, [])

    def _pasada(self, zonas: list[str]) -> dict[str, list[dict]]:
        cache = self._leer_cache()
        de_tarjetas = self._candidatos_de_tarjetas(zonas)
        del_sitemap = self._candidatos_del_sitemap()
        if not de_tarjetas and not del_sitemap:
            # Ni una tarjeta ni el sitemap: es el sitio o la red, no que
            # Mudafy se quedó sin avisos. No se cuenta como ausencia.
            log.warning("[mudafy] ni listados ni sitemap respondieron; no leo nada.")
            self.incomplete_zones.update(zonas)
            return {}

        cola = elegir_fichas(de_tarjetas, del_sitemap, cache, zonas, self.max_fichas)
        nuevas = sum(1 for sid, _ in cola if sid not in cache)
        log.info(
            "[mudafy] %d candidatos (%d de tarjetas, %d del sitemap), %d fichas "
            "conocidas. Pido %d: %d nuevas y %d para refrescar.",
            len(de_tarjetas.keys() | del_sitemap.keys()), len(de_tarjetas),
            len(del_sitemap), len(cache), len(cola), nuevas, len(cola) - nuevas,
        )

        por_zona: dict[str, list[dict]] = {z: [] for z in zonas}
        fallas = 0
        bajas = 0
        for i, (source_id, url) in enumerate(cola):
            if i:
                time.sleep(self.delay)
            try:
                respuesta = self.client.get(url)
            except httpx.HTTPError as exc:
                respuesta = None
                log.info("[mudafy] no pude pedir %s: %s", url, exc)

            if respuesta is not None and respuesta.status_code == 404:
                # Dado de baja. Se saca del caché para no volver a pedirlo;
                # en la base lo baja la regla de corridas sin verlo.
                cache.pop(source_id, None)
                bajas += 1
                fallas = 0
                continue
            if respuesta is None or respuesta.status_code != 200:
                fallas += 1
                if respuesta is not None:
                    log.info("[mudafy] %s respondió %s", url, respuesta.status_code)
                if fallas >= _MAX_FALLAS_SEGUIDAS:
                    # Algo nos está frenando. Se corta la pasada y no se
                    # cuenta como ausencia ningún aviso de esta corrida.
                    log.warning(
                        "[mudafy] %d fallas seguidas, corto en la ficha %d de %d.",
                        fallas, i + 1, len(cola),
                    )
                    self.incomplete_zones.update(zonas)
                    break
                continue
            fallas = 0

            item = parse_ficha(respuesta.text, str(respuesta.url))
            if item is None or item["source_id"] != source_id:
                # Redirigió a otra cosa (una búsqueda, otro aviso): el
                # aviso que pedimos ya no está.
                cache.pop(source_id, None)
                bajas += 1
                continue

            cache[source_id] = {
                "url": item["url"],
                "barrio": item.get("neighborhood"),
                "visto": _ahora(),
            }
            zona = zona_de_barrio(item.get("neighborhood"), zonas)
            if zona:
                item["zone"] = zona
                por_zona[zona].append(item)

            if (i + 1) % 50 == 0:
                self._guardar_cache(cache)

        self._guardar_cache(cache)
        self.capped_zones.update(zonas)
        log.info(
            "[mudafy] %d fichas de nuestras zonas, %d avisos ya no están.",
            sum(len(v) for v in por_zona.values()), bajas,
        )
        return por_zona

    # ---------------------------------------------------------------- #

    def _url_listado(self, property_slug: str, zone: str) -> str:
        return (
            f"{BASE}/{self.operation_slug}/{property_slug}/"
            f"{self.zone_prefix}{slug(zone)}"
        )

    def _es_del_tipo(self, url: str) -> bool:
        """Si la URL de una ficha es del tipo y la operación que buscamos.

        `/departamentos/guayaquil-720-departamento-en-venta-808429`: la
        carpeta dice el tipo y el final dice la operación. El sitemap trae de
        todo (casas, terrenos, cocheras, alquileres) y así se filtra sin
        pedir nada.
        """
        ruta = url.removeprefix(BASE)
        return any(ruta.startswith(f"/{p}/") for p in self.property_slugs) and bool(
            re.search(rf"-en-{re.escape(self.operation_slug)}-\d+$", ruta)
        )

    def _candidatos_de_tarjetas(self, zonas: list[str]) -> dict[str, str]:
        candidatos: dict[str, str] = {}
        primera = True
        for zona in zonas:
            for property_slug in self.property_slugs:
                if not primera:
                    time.sleep(self.delay)
                primera = False
                url = self._url_listado(property_slug, zona)
                try:
                    respuesta = self.client.get(url)
                except httpx.HTTPError as exc:
                    log.info("[mudafy] no pude leer el listado de %s: %s", zona, exc)
                    continue
                if respuesta.status_code == 404:
                    # Pasa con Belgrano R y Cañitas: Mudafy no tiene página
                    # de listado para esos barrios, pero sus fichas sí dicen
                    # "Belgrano R" y "Las Cañitas", así que llegan igual por
                    # el sitemap. Solo se pierden las tarjetas.
                    log.info(
                        "[mudafy] no hay listado para %s (%s); sus avisos llegan por el sitemap.",
                        zona, url,
                    )
                    continue
                if respuesta.status_code != 200:
                    log.info("[mudafy] el listado de %s respondió %s", zona, respuesta.status_code)
                    continue
                total = total_declarado(respuesta.text)
                if total is not None:
                    self.totals[zona] = self.totals.get(zona, 0) + total
                for href in re.findall(rf'href="(/{re.escape(property_slug)}/[a-z0-9-]+)"', respuesta.text):
                    completa = BASE + href
                    encontrado = _ID.search(href)
                    if encontrado and self._es_del_tipo(completa):
                        candidatos[encontrado.group(1)] = completa
        return candidatos

    def _candidatos_del_sitemap(self) -> dict[str, str]:
        if not self.sitemap:
            return {}
        urls = descargar(f"{BASE}/{self.sitemap}", self.client)
        candidatos = {}
        for url in urls:
            encontrado = _ID.search(url)
            if encontrado and self._es_del_tipo(url):
                candidatos[encontrado.group(1)] = url
        return candidatos

    def _leer_cache(self) -> dict[str, dict]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("[mudafy] caché de fichas ilegible (%s), arranco de cero.", exc)
            return {}

    def _guardar_cache(self, cache: dict[str, dict]) -> None:
        # A un temporal y después se reemplaza: si la corrida se corta a la
        # mitad de escribir, el caché anterior queda entero.
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.cache_path.with_suffix(".tmp")
        temporal.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        temporal.replace(self.cache_path)


def total_declarado(html: str) -> int | None:
    """El "1.195 departamentos en venta en CABA Palermo" del <title>.

    Mudafy declara bastante más de lo que su sitemap lista: 1.195 en Palermo
    contra ~1.370 departamentos en venta en todo el sitemap, el 23/09. Por
    eso vale guardarlo: es la única medida de cuánto nos falta.
    """
    match = re.search(r"<title>([^<]*)</title>", html)
    if not match or not re.match(r"\s*[\d.]+\s", match.group(1)):
        return None
    return parse_total(match.group(1))


def elegir_fichas(
    de_tarjetas: dict[str, str],
    del_sitemap: dict[str, str],
    cache: dict[str, dict],
    zonas: list[str],
    tope: int,
) -> list[tuple[str, str]]:
    """Qué fichas pedir esta corrida, en orden, hasta `tope`.

    1. Las de tarjetas que no conocemos: seguro son de nuestras zonas.
    2. Las del sitemap que no conocemos: la mayoría no lo son, pero hasta
       leerlas no se sabe.
    3. Las conocidas de nuestras zonas, de la que hace más que no vemos a la
       más reciente. Es lo que actualiza precios y arma el historial.

    Las conocidas de otras zonas no se piden nunca más: el barrio no cambia.
    Si mañana se agrega la zona al config, pasan solas al grupo 3.
    """
    cola: dict[str, str] = {}
    for candidatos in (de_tarjetas, del_sitemap):
        for source_id, url in candidatos.items():
            if source_id not in cache:
                cola.setdefault(source_id, url)

    conocidas = sorted(
        (
            (datos.get("visto", ""), source_id, datos["url"])
            for source_id, datos in cache.items()
            if datos.get("url") and zona_de_barrio(datos.get("barrio"), zonas)
        ),
    )
    for _, source_id, url in conocidas:
        cola.setdefault(source_id, url)

    return list(cola.items())[: max(0, tope)]


def parse_ficha(html: str, url: str) -> dict | None:
    """El aviso a partir del HTML de su ficha, o None si ya no está a la venta.

    Casi todo sale del JSON-LD (los datos estructurados de schema.org que el
    sitio publica para Google): precio, dirección, barrio, coordenadas y
    ambientes. Es la parte más estable de la página, porque si la cambian
    pierden posicionamiento.

    Lo que el JSON-LD no trae (superficie cubierta, año de construcción,
    expensas) sale de los datos que Next.js serializa en la página. Ahí
    también vienen los avisos "similares" que se muestran abajo, con su
    propia superficie y sus propias expensas: por eso se lee solo el tramo
    que va del `slug` de este aviso hasta el `slug` del siguiente.
    """
    encontrado = _ID.search(url.rstrip("/"))
    if not encontrado:
        return None
    source_id = encontrado.group(1)

    bloques: dict[str, dict] = {}
    for bloque in _LD_JSON.findall(html):
        try:
            datos = json.loads(bloque)
        except ValueError:
            continue
        if isinstance(datos, dict):
            bloques[datos.get("@type", "")] = datos

    producto = bloques.get("Product") or {}
    oferta = producto.get("offers") or {}
    if not oferta or "InStock" not in (oferta.get("availability") or "InStock"):
        return None
    # El inmueble viene como Apartment, House, SingleFamilyResidence...: se
    # toma el que tenga dirección.
    inmueble = next(
        (b for t, b in bloques.items() if t not in ("Product", "BreadcrumbList") and "address" in b),
        {},
    )
    direccion = inmueble.get("address") or {}
    migas = [
        e.get("name")
        for e in (bloques.get("BreadcrumbList") or {}).get("itemListElement", [])
    ]
    geo = inmueble.get("geo") or {}

    item: dict = {
        "id": f"mudafy:{source_id}",
        "source": "mudafy",
        "source_id": source_id,
        "url": url.split("?")[0],
        "title": (inmueble.get("name") or producto.get("name") or "").strip()
        or direccion.get("streetAddress"),
        "neighborhood": direccion.get("addressLocality"),
        # Migas: Inicio > Departamentos en venta > CABA > Caballito > ...
        "city": migas[2] if len(migas) > 3 else None,
        "address": direccion.get("streetAddress"),
        "price": _numero(oferta.get("price")),
        "currency": oferta.get("priceCurrency"),
        "rooms": _numero(inmueble.get("numberOfRooms")),
        "bedrooms": _numero(inmueble.get("numberOfBedrooms")),
        "bathrooms": _numero(inmueble.get("numberOfBathroomsTotal")),
        "latitude": _coordenada(geo.get("latitude")),
        "longitude": _coordenada(geo.get("longitude")),
    }

    texto = html.replace('\\"', '"')
    propio = re.search(rf'"id":(\d+),"slug":"[^"]*-{source_id}"', texto)
    if propio:
        siguiente = re.compile(r'"id":\d+,"slug":"').search(texto, propio.end())
        tramo = texto[propio.end(): siguiente.start() if siguiente else len(texto)]

        dimensiones = re.search(r'"dimensions":(\{[^{}]*\})', tramo)
        if dimensiones:
            try:
                d = json.loads(dimensiones.group(1))
            except ValueError:
                d = {}
            # Mudafy pone 0 donde no sabe: 0 m² no es un dato.
            item["covered_area"] = _numero(d.get("roofed_area")) or None
            item["total_area"] = _numero(d.get("total_area")) or None
            # Cubierta mayor que total es un error de tipeo del anunciante
            # (visto: 483 m² cubiertos sobre 48,3 totales). No se sabe cuál
            # de las dos está mal, pero la total es la que muestra la
            # tarjeta y la que se usaba antes: se descarta la cubierta.
            if (
                item["covered_area"] and item["total_area"]
                and item["covered_area"] > item["total_area"]
            ):
                item["covered_area"] = None

        anio = re.search(r'"construction_year":(\d{4})', tramo)
        if anio and 1800 < int(anio.group(1)) <= date.today().year:
            item["age_years"] = date.today().year - int(anio.group(1))

        # Solo en pesos: la columna es "en moneda local" (ver config). A
        # veces viene sin moneda (`"currency":null`) y la tarjeta la muestra
        # en pesos, así que null también cuenta como pesos.
        expensas = re.search(
            r'\{"name":"expenses","price":\{"amount":([\d.]+),"currency":(?:"ARS"|null)\}',
            tramo,
        )
        if expensas:
            item["maintenance_fee"] = _numero(expensas.group(1)) or None

        # Las fotos cuelgan de la publicación por su id interno, no por el
        # slug, y vienen antes del tramo: se cuentan en toda la página.
        item["photo_count"] = len(
            re.findall(
                rf'"resource_id":{propio.group(1)},"resource_type":"publications","type":"photo"',
                texto,
            )
        )
    if not item.get("photo_count") and (inmueble.get("image") or producto.get("image")):
        item["photo_count"] = 1

    return item


def _numero(valor) -> float | int | None:
    if valor is None or valor == "":
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return int(numero) if numero.is_integer() else numero


def _coordenada(valor) -> float | None:
    """Mudafy publica las coordenadas redondeadas a 3 decimales (~100 m).
    El round solo limpia el ruido de float (-34.599000000000004)."""
    numero = _numero(valor)
    return round(float(numero), 6) if numero else None


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build(conf: dict) -> MudafySource:
    return MudafySource(conf)
