"""Subtes, hospitales con guardia y comisarías, y qué tan cerca está cada aviso.

Los datos salen de `lugares.json`, que se baja con `scripts/bajar_lugares.py`
y está commiteado: el análisis y el mapa andan sin red.

La idea: el subte cerca suma (es la diferencia entre 10 minutos y 40 hasta el
centro) y estar pegado a un hospital con guardia o a una comisaría resta
(ambulancias, sirenas y movimiento a toda hora).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

ARCHIVO = Path(__file__).parent / "lugares.json"

# A escala de ciudad alcanza con tratar la esquina de un grado como un
# rectángulo: el error contra la fórmula de la esfera es de centímetros en
# pocos kilómetros, y acá se compara contra umbrales de cientos de metros.
_METROS_POR_GRADO_LAT = 110_574
_METROS_POR_GRADO_LON = 111_320


@lru_cache(maxsize=1)
def cargar(archivo: str | Path = ARCHIVO) -> dict:
    return json.loads(Path(archivo).read_text(encoding="utf-8"))


def distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metros entre dos puntos."""
    dy = (lat2 - lat1) * _METROS_POR_GRADO_LAT
    dx = (lon2 - lon1) * _METROS_POR_GRADO_LON * np.cos(np.radians((lat1 + lat2) / 2))
    return float(np.hypot(dx, dy))


def distancia_al_mas_cercano(lats, lons, lugares: list[dict]) -> np.ndarray:
    """Para cada aviso, metros hasta el lugar más cercano de la lista.

    Devuelve NaN donde el aviso no tiene coordenadas. Son ~200 lugares por
    ~2.000 avisos: la matriz entera entra en memoria sin problema.
    """
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    if not lugares or lats.size == 0:
        return np.full(lats.shape, np.nan)

    destino_lat = np.array([l["lat"] for l in lugares])
    destino_lon = np.array([l["lon"] for l in lugares])

    dy = (destino_lat[None, :] - lats[:, None]) * _METROS_POR_GRADO_LAT
    dx = (
        (destino_lon[None, :] - lons[:, None])
        * _METROS_POR_GRADO_LON
        * np.cos(np.radians(lats[:, None]))
    )
    return np.sqrt(dx**2 + dy**2).min(axis=1)


def puntaje_ubicacion(
    distancia_subte: np.ndarray,
    distancia_a_evitar: np.ndarray,
    subte_m: float,
    evitar_m: float,
) -> np.ndarray:
    """Factor 0-1 de qué tan bien ubicado está cada aviso. NaN si no se sabe.

    Dos partes: 70% cuánto falta caminar hasta el subte (lineal, 1 en la
    puerta y 0 a `subte_m`) y 30% no tener un hospital con guardia o una
    comisaría a menos de `evitar_m`. Separadas a propósito: si fuera una sola
    cuenta, un aviso lejos de todo puntuaría igual que uno pegado a la guardia
    de un hospital.
    """
    cerca_del_subte = np.clip(1 - distancia_subte / subte_m, 0, 1)
    tranquilo = np.where(np.isnan(distancia_a_evitar), 1.0, distancia_a_evitar >= evitar_m)
    return 0.7 * cerca_del_subte + 0.3 * tranquilo


def distancias(df, archivo: str | Path = ARCHIVO) -> dict[str, np.ndarray]:
    """Metros al subte más cercano y al hospital/comisaría más cercano."""
    lugares = cargar(archivo)
    vacio = np.full(len(df), np.nan)
    # Sin columnas de coordenadas (ej. un DataFrame armado a mano en un test)
    # la respuesta es "no sé", igual que un aviso sin ubicación.
    lats = df["latitude"].to_numpy(dtype=float) if "latitude" in df else vacio
    lons = df["longitude"].to_numpy(dtype=float) if "longitude" in df else vacio
    a_evitar = lugares["hospitales"] + lugares["comisarias"]
    return {
        "subte_m": distancia_al_mas_cercano(lats, lons, lugares["subte"]),
        "evitar_m": distancia_al_mas_cercano(lats, lons, a_evitar),
    }
