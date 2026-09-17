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
    url               TEXT,
    title             TEXT,
    zone              TEXT,
    neighborhood      TEXT,
    city              TEXT,
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

CREATE TABLE IF NOT EXISTS price_snapshots (
    listing_id  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    price       REAL,
    currency    TEXT,
    price_norm  REAL,
    PRIMARY KEY (listing_id, seen_at)
);

CREATE INDEX IF NOT EXISTS idx_snap_listing ON price_snapshots(listing_id);
"""

UPSERT_FIELDS = [
    "id", "source", "source_id", "url", "title", "zone", "neighborhood", "city",
    "latitude", "longitude", "price", "currency", "price_norm", "maintenance_fee",
    "covered_area", "total_area", "rooms", "bedrooms", "bathrooms", "age_years",
    "photo_count", "fingerprint", "raw",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


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
) -> int:
    """Los avisos de esta fuente que no aparecieron en la corrida se bajan.

    Un aviso que desaparece es señal: se vendió, o lo retiraron. Pero si
    `zones` viene dado, solo se consideran avisos de esas zonas: una zona que
    la fuente no pudo terminar de leer (bloqueo anti-bot, 403, etc.) no debe
    hacer que sus avisos reales se den de baja por no haber aparecido.
    """
    query = "SELECT id FROM listings WHERE source = ? AND active = 1"
    params: list = [source]
    if zones is not None:
        if not zones:
            return 0
        query += f" AND zone IN ({','.join('?' * len(zones))})"
        params.extend(zones)

    rows = conn.execute(query, params).fetchall()
    gone = [r["id"] for r in rows if r["id"] not in seen_ids]
    conn.executemany("UPDATE listings SET active = 0 WHERE id = ?", [(g,) for g in gone])
    return len(gone)
