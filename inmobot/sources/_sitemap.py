"""Descubrir URLs de búsqueda desde el sitemap que publica el portal.

Un sitemap es la lista de URLs que el sitio *quiere* que se crawleen: viene
declarada en su propio robots.txt, al lado de los Disallow. Es la forma más
limpia de pedir datos que existe, y además resuelve un problema real.

Armando la URL a mano solo se llega a `/departamentos/venta/palermo`, y de
ahí salen 60 avisos de los ~10.000 que Argenprop tiene en Palermo, porque el
robots.txt corta en la página 3. El sitemap muestra que el portal indexa
además `palermo-chico`, `palermo-hollywood`, `palermo-soho`, `palermo-nuevo`
y `palermo-viejo`: cada una es otra búsqueda, con sus propias 3 páginas.
Medido en Palermo, tres de esas rutas dan 145 avisos distintos contra 58.
"""

from __future__ import annotations

import gzip
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from ..normalize import slug

log = logging.getLogger(__name__)

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)


def _descomprimir(contenido: bytes) -> str:
    if contenido[:2] == b"\x1f\x8b":
        contenido = gzip.decompress(contenido)
    return contenido.decode("utf-8", "replace")


def _locs(texto: str) -> list[str]:
    """Las <loc> del XML, con una salida por regex si el XML viene roto."""
    try:
        raiz = ET.fromstring(texto)
    except ET.ParseError:
        return [m.strip() for m in _LOC.findall(texto)]
    espacio = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    return [
        (nodo.text or "").strip()
        for nodo in raiz.iter(f"{espacio}loc")
        if (nodo.text or "").strip()
    ]


def descargar(url: str, client: httpx.Client, max_indices: int = 10) -> list[str]:
    """Las URLs de un sitemap, siguiendo un nivel de índice si lo hubiera.

    Un sitemap grande se parte en varios y el archivo principal es un índice
    que apunta a los pedazos (Argenprop: `-01.xml.gz`, `-02.xml.gz`, ...).
    """
    try:
        respuesta = client.get(url)
        respuesta.raise_for_status()
        urls = _locs(_descomprimir(respuesta.content))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("[sitemap] no pude leer %s: %s", url, exc)
        return []

    hijos = [u for u in urls if ".xml" in u.rsplit("/", 1)[-1]]
    if not hijos:
        return urls

    salida: list[str] = []
    for hijo in hijos[:max_indices]:
        try:
            respuesta = client.get(hijo)
            respuesta.raise_for_status()
            salida.extend(_locs(_descomprimir(respuesta.content)))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("[sitemap] no pude leer %s: %s", hijo, exc)
    return salida


def _barrio_de(ruta: str, operation_slug: str) -> str | None:
    """El segmento que sigue a la operación: `/departamentos/venta/palermo-soho/monoambiente` -> `palermo-soho`."""
    partes = [p for p in ruta.split("/") if p]
    if operation_slug not in partes:
        return None
    i = partes.index(operation_slug)
    return partes[i + 1] if i + 1 < len(partes) else None


def rutas_por_zona(
    urls: list[str],
    zonas: list[str],
    property_slug: str,
    operation_slug: str,
    base: str,
) -> dict[str, list[str]]:
    """Agrupa las rutas del sitemap bajo la zona del config que les toca.

    Una zona se queda con su barrio y con los sub-barrios que empiezan
    igual: "Palermo" recoge `palermo-soho`, `palermo-hollywood`, etc.

    La única sutileza es que `belgrano-r` empieza con `belgrano`, y si
    "Belgrano R" también está en el config esa ruta le pertenece a ella. Por
    eso cada ruta se la queda la zona que la matchea de forma más
    específica, no la primera que pasa: si no, Belgrano R aparecería dos
    veces y sus avisos quedarían archivados bajo el barrio equivocado.
    """
    slugs = {z: slug(z) for z in zonas}
    agrupadas: dict[str, list[str]] = {z: [] for z in zonas}

    for url in urls:
        ruta = url[len(base):] if url.startswith(base) else url
        if not ruta.startswith(f"/{property_slug}/") or f"/{operation_slug}/" not in ruta:
            continue
        barrio = _barrio_de(ruta, operation_slug)
        if not barrio:
            continue

        candidatas = [
            z for z, s in slugs.items() if barrio == s or barrio.startswith(f"{s}-")
        ]
        if not candidatas:
            continue
        agrupadas[max(candidatas, key=lambda z: len(slugs[z]))].append(ruta)

    return {z: sorted(set(rutas)) for z, rutas in agrupadas.items()}


def descargar_con_navegador(url: str, page, destino: Path) -> list[str]:
    """Igual que `descargar`, pero pasando por Playwright.

    Zonaprop sirve sus sitemaps detrás de Cloudflare: por httpx dan 403, y
    por la API de red del navegador también (el WAF distingue la navegación
    de la petición de fondo). Navegando al archivo sí salen, solo que
    Chromium los toma como descarga porque vienen gzipeados — de ahí el
    `expect_download` en vez de leer el body.

    El índice (`sitemaps_https.xml`, sin comprimir) se lee como página
    normal; los pedazos (`.xml.gz`) se bajan a disco y se descomprimen.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        indice = _locs(page.content())
    except Exception as exc:
        log.warning("[sitemap] no pude leer el índice %s: %s", url, exc)
        return []

    partes = [u for u in indice if u.endswith(".xml.gz")]
    if not partes:
        return indice

    salida: list[str] = []
    destino.mkdir(parents=True, exist_ok=True)
    for i, parte in enumerate(partes):
        archivo = destino / f"sitemap-{i}.xml.gz"
        try:
            with page.expect_download(timeout=60000) as info:
                try:
                    page.goto(parte, timeout=40000)
                except Exception:
                    pass  # la navegación "falla" justamente porque descarga
            info.value.save_as(archivo)
            salida.extend(_locs(_descomprimir(archivo.read_bytes())))
        except Exception as exc:
            log.warning("[sitemap] no pude bajar %s: %s", parte, exc)
        finally:
            archivo.unlink(missing_ok=True)
    return salida


def rutas_por_zona_planas(
    urls: list[str],
    zonas: list[str],
    prefijo: str,
    base: str,
    prohibidas: tuple[str, ...] = (),
    permitidas: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    """Igual que `rutas_por_zona`, para portales de URL plana.

    Zonaprop no usa carpetas sino un slug corrido
    (`/departamentos-venta-botanico-palermo.html`), y el barrio no siempre
    está al principio: "Bajo Palermo", "Botánico Palermo" y "Barrio Parque
    Palermo" son todos Palermo. Por eso se busca el slug de la zona como
    secuencia de palabras en cualquier posición, y no como prefijo.

    `prohibidas` y `permitidas` implementan los pares Allow/Disallow del
    robots.txt: el sitemap lista URLs que el robots.txt igual no deja pedir
    (todos los `-orden-*` menos `-orden-precio-ascendente`), y manda el
    robots.txt.
    """
    slugs = {z: slug(z).split("-") for z in zonas}
    agrupadas: dict[str, list[str]] = {z: [] for z in zonas}

    for url in urls:
        ruta = url[len(base):] if url.startswith(base) else url
        if not ruta.startswith(prefijo):
            continue
        if any(p in ruta for p in prohibidas) and not any(a in ruta for a in permitidas):
            continue

        lugar = _solo_el_lugar(ruta[len(prefijo):].removesuffix(".html").split("-"))
        # La zona tiene que estar al FINAL del lugar, no en cualquier parte:
        # el sitemap es nacional y `belgrano-rosario` es el Belgrano de
        # Rosario, Santa Fe. Al final quedan el barrio o su ciudad, así que
        # `botanico-palermo` es Palermo y `belgrano-rosario` no es Belgrano.
        # Lo que esto NO distingue es otra localidad que termina con el
        # nombre del barrio: `villa-general-belgrano` (Córdoba) pasa igual
        # que `barrancas-de-belgrano`. Eso lo filtra la fuente mirando la
        # ubicación de las tarjetas (ver ZonapropSource._padres).
        candidatas = [z for z, s in slugs.items() if lugar[-len(s):] == s]
        # Y el caso del sub-barrio seguido de su barrio padre:
        # `belgrano-r-belgrano` es Belgrano R, pero termina en "belgrano".
        # Solo vale si lo que sigue al sub-barrio es otra zona del config,
        # que es justamente lo que `belgrano-rosario` no cumple.
        candidatas += [
            z for z, s in slugs.items()
            if lugar[:len(s)] == s
            and any(lugar[len(s):][-len(o):] == o for o in slugs.values())
        ]
        if not candidatas:
            continue
        agrupadas[max(candidatas, key=lambda z: len(slugs[z]))].append(ruta)

    return {z: sorted(set(rutas)) for z, rutas in agrupadas.items()}


# Lo que Zonaprop pega después del barrio y no es parte del lugar:
# "-2-habitaciones", "-con-balcon", "-orden-precio-ascendente",
# "-mas-de-5-habitaciones", "-monoambiente".
_ARRANQUE_DE_FILTRO = ("con", "orden", "mas", "monoambiente", "desde", "hasta")


def _solo_el_lugar(palabras: list[str]) -> list[str]:
    """Corta los filtros del final y deja el barrio.

    `["botanico","palermo","con","balcon"]` -> `["botanico","palermo"]`
    """
    for i, palabra in enumerate(palabras):
        if palabra.isdigit() or palabra in _ARRANQUE_DE_FILTRO:
            return palabras[:i] or palabras
    return palabras
