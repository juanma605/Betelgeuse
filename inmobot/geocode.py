"""Ubicar los avisos que publican la dirección pero no las coordenadas.

Remax y Mudafy traen lat/long en el listado; MercadoLibre y Argenprop traen
la dirección ("Avenida Belgrano 3700, Almagro"). Con el geocodificador de la
Ciudad esa dirección se convierte en un punto, y esos avisos entran al mapa y
al puntaje de ubicación.

Se usa el normalizador de direcciones del GCBA: es gratis, no pide clave,
está hecho para direcciones de CABA y devuelve WGS84 (el dataset de
hospitales, en cambio, publica el sistema propio de la Ciudad). Cada dirección
se consulta una sola vez: el resultado queda en la tabla geocode_cache,
incluso cuando no se pudo resolver.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

import httpx

from .normalize import BBOX_DEFAULT

log = logging.getLogger(__name__)

API = "https://servicios.usig.buenosaires.gob.ar/normalizar/"

# Recuadro de la búsqueda: si el geocodificador devuelve algo de afuera, es
# que entendió otra cosa. Sale de normalize para que el scrape y el
# geocodificador no discutan sobre dónde queda CABA.
LAT_MIN, LAT_MAX = BBOX_DEFAULT["lat_min"], BBOX_DEFAULT["lat_max"]
LON_MIN, LON_MAX = BBOX_DEFAULT["lon_min"], BBOX_DEFAULT["lon_max"]

_CALLE_Y_ALTURA = re.compile(r"^\s*(.+?\s+\d{1,5})\s*$")
# "Olazabal 2600 - Piso 2", "Zapata y Matienzo - Unidad 703"
_DESPUES_DEL_GUION = re.compile(r"\s+-\s+.*$")
# MercadoLibre escribe "Jerónimo Salguero Al 2300" ("al" de altura), y con esa
# palabra en el medio el normalizador contesta "calle inexistente".
_ALTURA = re.compile(r"\s+(?:al|altura)\s+(?=\d)", re.IGNORECASE)


def limpiar_direccion(texto: str | None) -> str | None:
    """"Mexico 4200, Piso 3, Almagro Sur" -> "Mexico 4200".

    Se queda con calle y altura: el piso y el barrio confunden al
    normalizador. Sin altura no se geocodifica — una calle entera puede tener
    treinta cuadras y el punto caería en cualquier lado.
    """
    if not texto:
        return None
    # El punto de "Av." confunde al normalizador; sacarlo deja espacios dobles.
    primera = _DESPUES_DEL_GUION.sub("", texto.split(",")[0])
    primera = re.sub(r"\s+", " ", primera.replace(".", " ")).strip()
    encontrado = _CALLE_Y_ALTURA.match(_ALTURA.sub(" ", primera))
    return encontrado.group(1) if encontrado else None


def en_caba(lat: float, lon: float) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def geocodificar(direccion: str, client: httpx.Client) -> tuple[float, float] | None:
    """Coordenadas de una dirección, o None si no se pudo resolver sin dudas."""
    try:
        respuesta = client.get(
            API, params={"direccion": f"{direccion}, CABA", "geocodificar": "true"}
        )
        respuesta.raise_for_status()
        opciones = respuesta.json().get("direccionesNormalizadas") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("[geocode] falló %s: %s", direccion, exc)
        return None

    # Con más de una opción el normalizador no sabe cuál es (calles con
    # nombres parecidos): mejor sin ubicación que en la cuadra equivocada.
    if len(opciones) != 1:
        return None

    coordenadas = opciones[0].get("coordenadas") or {}
    if str(coordenadas.get("srid")) != "4326":
        return None
    try:
        lat, lon = float(coordenadas["y"]), float(coordenadas["x"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if en_caba(lat, lon) else None


# Cada cuántas consultas se guarda el avance. Con 25 se pierde medio
# minuto de trabajo en el peor caso, que es barato frente a commitear en cada
# vuelta.
CADA_CUANTO_GUARDA = 25


def completar_coordenadas(conn: sqlite3.Connection, cfg: dict, client=None) -> dict:
    """Geocodifica los avisos activos con dirección y sin coordenadas."""
    pendientes = conn.execute(
        "SELECT id, address FROM listings "
        "WHERE active = 1 AND latitude IS NULL AND address IS NOT NULL"
    ).fetchall()

    cache = {
        fila["address"]: (fila["lat"], fila["lon"])
        for fila in conn.execute("SELECT address, lat, lon FROM geocode_cache")
    }
    espera = float(cfg.get("rate_limit_seconds", 1.0))
    quedan = int(cfg.get("max_per_run", 300))
    stats = {"ubicados": 0, "consultadas": 0, "sin_resultado": 0}
    propio = client is None
    client = client or httpx.Client(
        headers={"User-Agent": "inmobot/0.1 (proyecto personal)"}, timeout=30.0
    )

    try:
        for fila in pendientes:
            direccion = limpiar_direccion(fila["address"])
            if not direccion:
                continue
            if direccion not in cache:
                if quedan <= 0:
                    continue
                quedan -= 1
                stats["consultadas"] += 1
                punto = geocodificar(direccion, client)
                if punto is None:
                    stats["sin_resultado"] += 1
                cache[direccion] = punto or (None, None)
                conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache (address, lat, lon, tried_at) "
                    "VALUES (?, ?, ?, ?)",
                    (direccion, cache[direccion][0], cache[direccion][1],
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                # Commit cada tanto y no solo al final: una corrida de
                # miles de direcciones son 40 minutos, y si el proceso se
                # corta antes (se cierra la laptop, se reinicia la máquina)
                # todo lo consultado se perdía y había que volver a
                # preguntárselo al normalizador desde cero.
                if stats["consultadas"] % CADA_CUANTO_GUARDA == 0:
                    conn.commit()
                time.sleep(espera)

            lat, lon = cache[direccion]
            if lat is not None:
                conn.execute(
                    "UPDATE listings SET latitude = ?, longitude = ? WHERE id = ?",
                    (lat, lon, fila["id"]),
                )
                stats["ubicados"] += 1
    finally:
        conn.commit()
        if propio:
            client.close()

    stats["pendientes"] = len(pendientes) - stats["ubicados"]
    return stats
