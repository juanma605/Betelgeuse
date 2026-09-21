"""Persistencia en SQLite.

Dos tablas:
  listings         -> estado actual de cada aviso (upsert en cada corrida)
  price_snapshots  -> una fila por aviso y por corrida en que cambió el precio

La segunda es la que vale oro: con dos meses de corridas sabés qué avisos
bajaron, cuántas veces y cuánto llevan publicados.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                TEXT PRIMARY KEY,      -- "<source>:<id nativo>"
    source            TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    operation         TEXT NOT NULL DEFAULT 'venta',  -- venta | alquiler
    url               TEXT,
    title             TEXT,
    zone              TEXT,
    neighborhood      TEXT,
    city              TEXT,
    address           TEXT,                  -- calle y altura, para geocodificar
    latitude          REAL,
    longitude         REAL,
    price             REAL,                  -- en la moneda original
    currency          TEXT,
    price_norm        REAL,                  -- normalizado a search.currency
    maintenance_fee   REAL,
    covered_area      REAL,
    total_area        REAL,
    rooms             INTEGER,
    bedrooms          INTEGER,
    bathrooms         INTEGER,
    age_years         INTEGER,
    photo_count       INTEGER,
    fingerprint       TEXT,                  -- para deduplicar entre portales
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    raw               TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_zone        ON listings(zone);
CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_active      ON listings(active);
CREATE INDEX IF NOT EXISTS idx_listings_operation   ON listings(operation);

CREATE TABLE IF NOT EXISTS price_snapshots (
    listing_id  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    price       REAL,
    currency    TEXT,
    price_norm  REAL,
    PRIMARY KEY (listing_id, seen_at)
);

CREATE INDEX IF NOT EXISTS idx_snap_listing ON price_snapshots(listing_id);

-- Una dirección se geocodifica una sola vez, aunque la repitan varios avisos
-- o aparezca en corridas siguientes. Guarda también las que no se pudieron
-- resolver (lat/lon en NULL) para no volver a preguntar por ellas.
CREATE TABLE IF NOT EXISTS geocode_cache (
    address   TEXT PRIMARY KEY,
    lat       REAL,
    lon       REAL,
    tried_at  TEXT NOT NULL
);
"""

UPSERT_FIELDS = [
    "id", "source", "source_id", "operation", "url", "title", "zone", "neighborhood", "city", "address",
    "latitude", "longitude", "price", "currency", "price_norm", "maintenance_fee",
    "covered_area", "total_area", "rooms", "bedrooms", "bathrooms", "age_years",
    "photo_count", "fingerprint", "raw",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connect(path: str | Path, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Una conexión a la base, en modo WAL.

    WAL deja que un escritor y varios lectores convivan. Sin él, el scrape
    tomaba la base entera mientras insertaba y el dashboard no podía ni
    leerla: `OperationalError: database is locked`, en medio de una corrida
    larga. Es una propiedad del archivo, así que basta con setearlo una vez.

    `readonly=True` abre sin permiso de escritura y sin tocar el esquema:
    para leer no hace falta crear tablas ni migrar columnas, y justamente
    ese `CREATE TABLE IF NOT EXISTS` era lo que pedía el lock. Es lo que
    usa el dashboard.
    """
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    # El timeout es para el caso inverso: dos escritores (el cron de las 7 y
    # una corrida a mano). En vez de fallar en el acto, espera su turno.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _agregar_columnas_nuevas(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _agregar_columnas_nuevas(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS no agrega columnas a una tabla que ya existe:
    sin esto, una base vieja se rompe al insertar una columna nueva."""
    existentes = {fila["name"] for fila in conn.execute("PRAGMA table_info(listings)")}
    for columna, tipo in (
        ("address", "TEXT"),
        ("missed_runs", "INTEGER DEFAULT 0"),
        ("operation", "TEXT NOT NULL DEFAULT 'venta'"),
    ):
        if columna not in existentes:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {columna} {tipo}")


def upsert_listings(
    conn: sqlite3.Connection,
    listings: Iterable[dict],
    keep_snapshots: bool = True,
) -> dict[str, int]:
    """Inserta o actualiza avisos. Devuelve contadores para el log."""
    stamp = now_iso()
    stats = {"new": 0, "updated": 0, "price_changes": 0}

    for item in listings:
        prev = conn.execute(
            "SELECT price_norm FROM listings WHERE id = ?", (item["id"],)
        ).fetchone()

        values = {field: item.get(field) for field in UPSERT_FIELDS}
        # Un aviso que no declara operación es una venta: es lo único que
        # hubo hasta que se agregaron los alquileres, y la columna es NOT
        # NULL justamente para que nada quede en el limbo entre las dos.
        values["operation"] = values["operation"] or "venta"

        if prev is None:
            columns = ", ".join(UPSERT_FIELDS + ["first_seen", "last_seen", "active"])
            holders = ", ".join(["?"] * (len(UPSERT_FIELDS) + 3))
            conn.execute(
                f"INSERT INTO listings ({columns}) VALUES ({holders})",
                [values[f] for f in UPSERT_FIELDS] + [stamp, stamp, 1],
            )
            stats["new"] += 1
        else:
            assignments = ", ".join(f"{f} = ?" for f in UPSERT_FIELDS if f != "id")
            conn.execute(
                f"UPDATE listings SET {assignments}, last_seen = ?, active = 1 "
                "WHERE id = ?",
                [values[f] for f in UPSERT_FIELDS if f != "id"] + [stamp, item["id"]],
            )
            stats["updated"] += 1
            if prev["price_norm"] != item.get("price_norm"):
                stats["price_changes"] += 1

        if keep_snapshots:
            conn.execute(
                "INSERT OR REPLACE INTO price_snapshots "
                "(listing_id, seen_at, price, currency, price_norm) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item["id"], stamp, item.get("price"),
                    item.get("currency"), item.get("price_norm"),
                ),
            )

    return stats


def mark_inactive(
    conn: sqlite3.Connection,
    seen_ids: set[str],
    source: str,
    zones: list[str] | None = None,
    operation: str = "venta",
) -> int:
    """Los avisos de esta fuente que no aparecieron en la corrida se bajan.

    Un aviso que desaparece es señal: se vendió, o lo retiraron. Pero si
    `zones` viene dado, solo se consideran avisos de esas zonas: una zona que
    la fuente no pudo terminar de leer (bloqueo anti-bot, 403, etc.) no debe
    hacer que sus avisos reales se den de baja por no haber aparecido.

    `operation` es lo que impide que una corrida de alquileres dé de baja
    todas las ventas de esa fuente: los alquileres jamás van a estar en
    `seen_ids` de un scrape de ventas, ni al revés.
    """
    query = "SELECT id FROM listings WHERE source = ? AND operation = ? AND active = 1"
    params: list = [source, operation]
    if zones is not None:
        if not zones:
            return 0
        query += f" AND zone IN ({','.join('?' * len(zones))})"
        params.extend(zones)

    rows = conn.execute(query, params).fetchall()
    gone = [r["id"] for r in rows if r["id"] not in seen_ids]
    conn.executemany("UPDATE listings SET active = 0 WHERE id = ?", [(g,) for g in gone])
    return len(gone)


def mark_inactive_after_misses(
    conn: sqlite3.Connection,
    seen_ids: set[str],
    source: str,
    zones: list[str],
    max_misses: int,
    operation: str = "venta",
) -> int:
    """Baja los avisos que llevan `max_misses` corridas seguidas sin aparecer.

    Es la versión paciente de `mark_inactive`, para las fuentes que no pueden
    ver la zona entera. Ahí una sola ausencia no prueba nada: el aviso pudo
    haberse vendido, o haber quedado fuera de las páginas que el robots.txt
    nos deja mirar, y el orden de esas páginas se mueve solo. Pero un aviso
    que no aparece en siete corridas seguidas ya no es mala suerte del orden.

    El contador se reinicia apenas el aviso vuelve a verse, así que un
    listado que rota no lo acumula nunca.
    """
    if not zones or max_misses <= 0:
        return 0

    marcas = ",".join("?" * len(zones))
    filas = conn.execute(
        f"SELECT id FROM listings WHERE source = ? AND operation = ? AND active = 1 "
        f"AND zone IN ({marcas})",
        [source, operation, *zones],
    ).fetchall()

    conn.executemany(
        "UPDATE listings SET missed_runs = 0 WHERE id = ?",
        [(r["id"],) for r in filas if r["id"] in seen_ids],
    )
    conn.executemany(
        "UPDATE listings SET missed_runs = COALESCE(missed_runs, 0) + 1 WHERE id = ?",
        [(r["id"],) for r in filas if r["id"] not in seen_ids],
    )
    cursor = conn.execute(
        f"UPDATE listings SET active = 0 WHERE source = ? AND operation = ? "
        f"AND active = 1 AND zone IN ({marcas}) AND missed_runs >= ?",
        [source, operation, *zones, max_misses],
    )
    return cursor.rowcount
