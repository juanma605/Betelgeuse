"""Baja subtes, bomberos y hospitales con guardia, y arma inmobot/lugares.json.

Se corre a mano cada tanto (las estaciones de subte y los cuarteles cambian
poco). El resultado se commitea: así el análisis y el mapa funcionan sin red,
igual que el dataset de demo.

    python scripts/bajar_lugares.py

Fuentes:
- Subte y bomberos: portal de datos abiertos de la Ciudad (CC-BY-2.5-AR).
  Traen la ubicación en WKT y en lat/long, que es lo que necesitamos.
- Hospitales: OpenStreetMap (ODbL). El dataset oficial de la Ciudad publica
  las coordenadas en el sistema propio de CABA (EPSG:9498) y convertirlo sin
  una librería de proyecciones es adivinar. Además acá solo interesan los que
  tienen guardia: son los que traen ambulancias y sirenas a toda hora.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import date
from pathlib import Path

import httpx

SALIDA = Path(__file__).resolve().parent.parent / "inmobot" / "lugares.json"
UA = {"User-Agent": "inmobot/0.1 (proyecto personal)"}  # sin tildes: las cabeceras HTTP son ASCII

CABA = "https://cdn.buenosaires.gob.ar/datosabiertos/datasets"
SUBTE_CSV = f"{CABA}/sbase/subte-estaciones/estaciones_de_subte.csv"
BOMBEROS_CSV = (
    f"{CABA}/ministerio-de-justicia-y-seguridad/cuarteles-destacamentos-bomberos/"
    "cuarteles_destacamentos_bomberos.csv"
)
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
BBOX = "-34.706,-58.531,-34.526,-58.335"  # CABA

_PUNTO = re.compile(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)")


def _desde_wkt(texto: str) -> tuple[float, float] | None:
    """WKT viene (long lat); nosotros guardamos (lat, long)."""
    encontrado = _PUNTO.search(texto or "")
    if not encontrado:
        return None
    lon, lat = float(encontrado.group(1)), float(encontrado.group(2))
    return lat, lon


def _csv(url: str) -> list[dict]:
    texto = httpx.get(url, headers=UA, timeout=90, follow_redirects=True).text
    # El portal manda BOM en algunos CSV y ensucia el nombre de la 1ra columna.
    return list(csv.DictReader(io.StringIO(texto.lstrip("﻿"))))


def subte() -> list[dict]:
    salida = []
    for fila in _csv(SUBTE_CSV):
        punto = _desde_wkt(fila.get("geometry", ""))
        if punto:
            linea = fila.get("linea", "").strip()
            salida.append({
                "nombre": f"{fila['estacion'].strip()} (línea {linea})" if linea else fila["estacion"],
                "lat": punto[0], "lon": punto[1],
            })
    return salida


def bomberos() -> list[dict]:
    salida = []
    for fila in _csv(BOMBEROS_CSV):
        punto = _desde_wkt(fila.get("geometry", ""))
        if punto:
            nombre = (fila.get("dcia") or fila.get("tipo") or "Bomberos").strip()
            salida.append({"nombre": nombre, "lat": punto[0], "lon": punto[1]})
    return salida


def hospitales_con_guardia() -> list[dict]:
    consulta = (
        f'[out:json][timeout:90];('
        f'node["amenity"="hospital"]["emergency"="yes"]({BBOX});'
        f'way["amenity"="hospital"]["emergency"="yes"]({BBOX});'
        f');out center tags;'
    )
    # Overpass devuelve 504 cuando está cargado: se reintenta y se cambia de
    # espejo antes de darlo por perdido.
    elementos = None
    for intento in range(4):
        url = OVERPASS[intento % len(OVERPASS)]
        try:
            respuesta = httpx.post(url, data={"data": consulta}, headers=UA, timeout=180)
            respuesta.raise_for_status()
            elementos = respuesta.json()["elements"]
            break
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            print(f"  overpass {url.split('/')[2]} falló ({type(exc).__name__}), reintento...")
            time.sleep(10)
    if elementos is None:
        raise SystemExit("Overpass no contestó: probá de nuevo en un rato.")
    salida = []
    for elemento in elementos:
        centro = elemento.get("center") or elemento
        if "lat" in centro:
            salida.append({
                "nombre": elemento.get("tags", {}).get("name", "Hospital"),
                "lat": centro["lat"], "lon": centro["lon"],
            })
    return salida


def main() -> None:
    datos = {
        "actualizado": date.today().isoformat(),
        "fuentes": {
            "subte": "Datos Abiertos GCBA (CC-BY-2.5-AR) — subte-estaciones",
            "bomberos": "Datos Abiertos GCBA (CC-BY-2.5-AR) — cuarteles-destacamentos-bomberos",
            "hospitales": "OpenStreetMap (ODbL) — amenity=hospital + emergency=yes",
        },
        "subte": subte(),
        "hospitales": hospitales_con_guardia(),
        "bomberos": bomberos(),
    }
    for clave in ("subte", "hospitales", "bomberos"):
        if not datos[clave]:
            raise SystemExit(f"{clave} vino vacío: no piso {SALIDA.name} con datos incompletos.")
        print(f"{clave}: {len(datos[clave])}")
    SALIDA.write_text(
        json.dumps(datos, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print("escrito", SALIDA)


if __name__ == "__main__":
    main()
