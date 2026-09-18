"""Exportación del dataset de demo: que anonimice y que no rompa el historial."""

import hashlib
import sqlite3

from inmobot import db, demo


def base_de_prueba(path) -> None:
    with db.connect(path) as conn:
        db.upsert_listings(conn, [
            {
                "id": "zonaprop:111", "source": "zonaprop", "source_id": "111",
                "url": "https://www.zonaprop.com.ar/propiedades/111.html",
                "title": "Emprendimiento en pozo, 2 ambientes a estrenar",
                "zone": "Almagro", "neighborhood": "Almagro Sur", "city": "CABA",
                "latitude": -34.6065431, "longitude": -58.4201234,
                "price_norm": 115_000, "covered_area": 48, "rooms": 2,
                "fingerprint": "2ed318b6f4ba8ac1",
                "raw": '{"secreto": "json crudo del portal"}',
            },
            {
                "id": "remax:222", "source": "remax", "source_id": "222",
                "url": "https://www.remax.com.ar/listings/222",
                "title": "Venta departamento dos ambientes", "zone": "Belgrano",
                "price_norm": 130_000, "covered_area": 55, "rooms": 3,
            },
        ])


def test_export_anonimiza_y_conserva_el_historial(tmp_path):
    base_de_prueba(tmp_path / "real.db")
    salida = tmp_path / "demo.db"

    stats = demo.export(tmp_path / "real.db", salida)
    assert stats == {"listings": 2, "snapshots": 2, "zones": 2}

    conn = sqlite3.connect(salida)
    conn.row_factory = sqlite3.Row
    filas = conn.execute("SELECT * FROM listings ORDER BY id").fetchall()

    assert [f["id"] for f in filas] == ["demo:0001", "demo:0002"]
    # El JSON crudo es contenido del portal y se va; la URL es un puntero a
    # una página pública y se queda.
    assert all(f["raw"] is None for f in filas)
    assert filas[1]["url"] == "https://www.zonaprop.com.ar/propiedades/111.html"

    # La numeración sigue el orden del id original, así que "remax:222" queda
    # antes que "zonaprop:111".
    pozo = filas[1]
    # Nada del título original sobrevive, pero sí la clasificación de pozo:
    # sin ella los emprendimientos volverían a contaminar las medianas.
    assert pozo["title"] == "2 amb · 48 m² · Almagro Sur · emprendimiento en pozo"
    assert pozo["latitude"] == -34.607
    # Lo estructural queda intacto.
    assert (pozo["price_norm"], pozo["covered_area"]) == (115_000, 48)
    # El fingerprint se conserva tal cual: es lo que hace que el demo siga
    # sirviendo para detectar el mismo inmueble publicado por dos agencias.
    assert pozo["fingerprint"] == "2ed318b6f4ba8ac1"

    huerfanos = conn.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE listing_id NOT IN (SELECT id FROM listings)"
    ).fetchone()[0]
    assert huerfanos == 0


def test_export_es_reproducible(tmp_path):
    # Si no fuera determinístico, regenerar el demo ensuciaría el diff del
    # repo con un binario distinto cada vez.
    base_de_prueba(tmp_path / "real.db")

    def exportar(nombre):
        demo.export(tmp_path / "real.db", tmp_path / nombre)
        return hashlib.sha1((tmp_path / nombre).read_bytes()).hexdigest()

    assert exportar("a.db") == exportar("b.db")
