"""Dataset de demo: una copia anonimizada de la base real, commiteable.

El repo es público y funciona como portfolio. Quien lo evalúa no tiene
credenciales de MercadoLibre, no va a instalar Playwright y le va a dedicar
cinco minutos: sin una base adentro del repo, el proyecto es imposible de
probar desde afuera.

Lo que el demo conserva es la **estructura** (precios, m², ambientes, zonas,
fechas y todo el historial de precios); lo que tira es el **contenido** de los
portales: título, URL y JSON crudo. Republicar avisos ajenos no hace falta
para demostrar el análisis, y es justo lo que el proyecto dice que no hace.

Regenerarlo cuando haya más historial es un comando y un commit:

    python -m inmobot demo-export
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import db
from .analyze import is_off_plan

log = logging.getLogger(__name__)

DEMO_DB_PATH = "data/demo.db"

LISTING_COLUMNS = db.UPSERT_FIELDS + ["first_seen", "last_seen", "active"]

# ~100 m de precisión: alcanza para que el mapa se vea poblado y no alcanza
# para identificar la unidad.
COORD_DECIMALS = 3


def db_path(cfg, demo: bool) -> str:
    """El demo ignora storage.path a propósito: es el mismo código corriendo
    contra otra base, no un modo de ejecución paralelo."""
    return DEMO_DB_PATH if demo else cfg.get_path("storage.path")


def synthetic_title(row) -> str:
    """Etiqueta armada con los campos numéricos del propio aviso.

    No es el título del portal ni una paráfrasis: se reconstruye desde cero
    con datos que igual están en las otras columnas ("2 amb · 48 m² ·
    Almagro"). El sufijo de pozo es lo único que agrega información: sin él
    se perdería la clasificación off_plan, que se calcula sobre el título y
    es la que mantiene los emprendimientos afuera de las medianas.
    """
    parts = []
    if row["rooms"]:
        parts.append(f"{int(row['rooms'])} amb")
    area = row["covered_area"] or row["total_area"]
    if area:
        parts.append(f"{int(area)} m²")
    place = row["neighborhood"] or row["zone"]
    if place:
        parts.append(str(place))
    # La URL se borra en el demo, así que la marca de pozo tiene que quedar
    # en el título: es el único lugar donde sobrevive la clasificación.
    if is_off_plan(row["title"], row["url"]):
        parts.append("emprendimiento en pozo")
    return " · ".join(parts) or "aviso"


def _anonymize(row, new_id: str) -> dict:
    item = {field: row[field] for field in LISTING_COLUMNS}
    item["id"] = new_id
    item["source_id"] = new_id
    item["title"] = synthetic_title(row)
    item["url"] = None
    item["raw"] = None
    for coord in ("latitude", "longitude"):
        if item[coord] is not None:
            item[coord] = round(item[coord], COORD_DECIMALS)
    return item


def _sample(rows: list, limit: int | None) -> list:
    """Muestreo estratificado por zona, conservando grupos zona/ambientes enteros.

    Un muestreo plano sesga el demo hacia el barrio con más avisos, y como
    `zone_stats` exige `min_comparables` por grupo, partir grupos al medio
    puede dejar a todos por debajo del mínimo y producir un demo donde el
    análisis no devuelve nada. Por eso los grupos entran completos y el
    límite es aproximado: se prioriza que los grupos sirvan.
    """
    if limit is None or len(rows) <= limit:
        return rows

    groups: dict = {}
    for row in rows:
        groups.setdefault(row["zone"], {}).setdefault(row["rooms"], []).append(row)

    # Grupos grandes primero dentro de cada zona (son los que llegan al
    # mínimo de comparables); el desempate por ambientes es solo para que
    # dos corridas sobre la misma base den exactamente lo mismo.
    queues = {
        zone: sorted(by_rooms.values(), key=lambda g: (-len(g), str(g[0]["rooms"])))
        for zone, by_rooms in groups.items()
    }
    zones = sorted(queues, key=lambda z: (z is None, z or ""))

    picked: list = []
    while len(picked) < limit and any(queues[z] for z in zones):
        for zone in zones:
            if not queues[zone]:
                continue
            picked.extend(queues[zone].pop(0))
            if len(picked) >= limit:
                break
    return picked


def export(src_path: str | Path, out_path: str | Path, limit: int | None = None) -> dict:
    """Escribe una copia anonimizada de `src_path` en `out_path`.

    Determinístico: nada al azar, los avisos se numeran por orden de id
    original. Correrlo dos veces sobre la misma base da el mismo archivo.
    """
    src_path, out_path = Path(src_path), Path(out_path)
    if not src_path.exists():
        raise FileNotFoundError(f"No encuentro la base real en {src_path}.")

    source = sqlite3.connect(src_path)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("SELECT * FROM listings ORDER BY id").fetchall()
        picked = sorted(_sample(rows, limit), key=lambda r: r["id"])
        id_map = {row["id"]: f"demo:{i:04d}" for i, row in enumerate(picked, start=1)}

        snapshots = [
            (id_map[row["listing_id"]], row["seen_at"], row["price"],
             row["currency"], row["price_norm"])
            for row in source.execute(
                "SELECT * FROM price_snapshots ORDER BY listing_id, seen_at"
            )
            if row["listing_id"] in id_map
        ]
    finally:
        source.close()

    out_path.unlink(missing_ok=True)
    columns = ", ".join(LISTING_COLUMNS)
    holders = ", ".join(["?"] * len(LISTING_COLUMNS))
    with db.connect(out_path) as conn:
        for row in picked:
            item = _anonymize(row, id_map[row["id"]])
            conn.execute(
                f"INSERT INTO listings ({columns}) VALUES ({holders})",
                [item[field] for field in LISTING_COLUMNS],
            )
        conn.executemany(
            "INSERT INTO price_snapshots "
            "(listing_id, seen_at, price, currency, price_norm) VALUES (?, ?, ?, ?, ?)",
            snapshots,
        )
        orphans = conn.execute(
            "SELECT COUNT(*) FROM price_snapshots WHERE listing_id NOT IN "
            "(SELECT id FROM listings)"
        ).fetchone()[0]

    # Si la renumeración rompiera la relación, el historial del demo quedaría
    # huérfano y `price_drops` devolvería vacío sin que se note por qué.
    if orphans:
        raise RuntimeError(f"{orphans} snapshots quedaron sin aviso: revisá el id_map.")

    return {
        "listings": len(picked),
        "snapshots": len(snapshots),
        "zones": len({row["zone"] for row in picked}),
    }
