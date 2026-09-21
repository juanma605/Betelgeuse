"""Regresión del bug de mark_inactive.

Cuando Cloudflare cortaba una zona a mitad de camino, los avisos de esa zona
no llegaban a `seen_ids` y se daban de baja como si se hubieran vendido. Una
corrida bloqueada borraba medio barrio de la base. El arreglo fue pasarle a
`mark_inactive` solo las zonas que la fuente terminó de leer; este test está
para que no vuelva.
"""

import sqlite3

import pytest

from inmobot import db


def poblar(conn, zona: str, cantidad: int, desde: int = 0) -> list[str]:
    avisos = [
        {
            "id": f"zonaprop:{zona}-{i}",
            "source": "zonaprop",
            "source_id": str(i),
            "zone": zona,
            "price_norm": 100_000,
        }
        for i in range(desde, desde + cantidad)
    ]
    db.upsert_listings(conn, avisos)
    return [a["id"] for a in avisos]


def activos(conn, zona: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM listings WHERE zone = ? AND active = 1", (zona,)
    ).fetchone()[0]


def test_una_zona_incompleta_no_da_de_baja_sus_avisos(tmp_path):
    with db.connect(tmp_path / "t.db") as conn:
        bloqueada = poblar(conn, "Almagro", 50)
        poblar(conn, "Belgrano", 50, desde=100)

        # Cloudflare cortó Almagro en la primera página: solo se vieron 3.
        vistos = set(bloqueada[:3]) | set(poblar(conn, "Belgrano", 50, desde=100))
        bajas = db.mark_inactive(conn, vistos, "zonaprop", zones=["Belgrano"])

        assert bajas == 0
        assert activos(conn, "Almagro") == 50


def test_en_una_zona_completa_los_que_faltan_si_se_dan_de_baja(tmp_path):
    with db.connect(tmp_path / "t.db") as conn:
        belgrano = poblar(conn, "Belgrano", 50)

        # Belgrano terminó bien y 10 avisos ya no aparecen: se vendieron o
        # los retiraron. Esos sí se bajan.
        db.mark_inactive(conn, set(belgrano[:40]), "zonaprop", zones=["Belgrano"])

        assert activos(conn, "Belgrano") == 40



def test_en_zona_con_tope_el_aviso_aguanta_siete_corridas(tmp_path):
    """Las fuentes que no llegan al final de la lista no pueden leer una
    ausencia como una venta: el robots.txt de Zonaprop deja ver 5 páginas de
    las 575 que tiene Palermo, y ese orden se mueve solo. El 21/09 eso dio
    de baja 815 avisos que seguían publicados.

    Pero tampoco puede ser que un vendido quede activo para siempre. Siete
    corridas seguidas sin aparecer ya no son mala suerte del orden.
    """
    with db.connect(tmp_path / "t.db") as conn:
        avisos = poblar(conn, "Palermo", 3)
        siempre, intermitente, vendido = avisos

        for corrida in range(1, 7):
            # El intermitente entra y sale del top-150 según el orden del día.
            vistos = {siempre} | ({intermitente} if corrida % 2 else set())
            bajas = db.mark_inactive_after_misses(
                conn, vistos, "zonaprop", ["Palermo"], max_misses=7
            )
            assert bajas == 0, f"nadie se baja en la corrida {corrida}"
            assert activos(conn, "Palermo") == 3

        # Séptima ausencia seguida del que nunca volvió: ese sí se baja.
        bajas = db.mark_inactive_after_misses(
            conn, {siempre, intermitente}, "zonaprop", ["Palermo"], max_misses=7
        )
        assert bajas == 1
        vivos = {r[0] for r in conn.execute("SELECT id FROM listings WHERE active = 1")}
        assert vivos == {siempre, intermitente}
        assert vendido not in vivos


def test_el_contador_se_reinicia_cuando_el_aviso_reaparece(tmp_path):
    """Un aviso que rota dentro y fuera de la ventana no acumula nunca: si
    no, bastaría con perderlo salteado siete veces para matarlo."""
    with db.connect(tmp_path / "t.db") as conn:
        (aviso,) = poblar(conn, "Palermo", 1)

        for _ in range(6):
            db.mark_inactive_after_misses(conn, set(), "zonaprop", ["Palermo"], 7)
        assert activos(conn, "Palermo") == 1

        db.mark_inactive_after_misses(conn, {aviso}, "zonaprop", ["Palermo"], 7)
        contador = conn.execute(
            "SELECT missed_runs FROM listings WHERE id = ?", (aviso,)
        ).fetchone()[0]
        assert contador == 0

        # Y desde cero le vuelven a hacer falta las siete.
        for _ in range(6):
            db.mark_inactive_after_misses(conn, set(), "zonaprop", ["Palermo"], 7)
        assert activos(conn, "Palermo") == 1


def test_con_max_misses_en_cero_no_se_da_de_baja_nunca(tmp_path):
    """El interruptor del config para volver al comportamiento conservador."""
    with db.connect(tmp_path / "t.db") as conn:
        poblar(conn, "Palermo", 2)
        for _ in range(20):
            assert db.mark_inactive_after_misses(conn, set(), "zonaprop", ["Palermo"], 0) == 0
        assert activos(conn, "Palermo") == 2


def test_se_puede_leer_mientras_otro_escribe(tmp_path):
    """El dashboard reventaba con `database is locked` en medio de un scrape
    largo. Eran dos cosas encadenadas: la base no estaba en WAL, así que el
    escritor excluía a todos, y el dashboard además abría en modo escritura
    porque `connect` corre `CREATE TABLE IF NOT EXISTS` al entrar.
    """
    ruta = tmp_path / "t.db"
    with db.connect(ruta) as conn:
        poblar(conn, "Palermo", 3)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    # Un escritor con la transacción abierta, como el scrape entre fuentes.
    with db.connect(ruta) as escritor:
        poblar(escritor, "Almagro", 2)

        with db.connect(ruta, readonly=True) as lector:
            vistos = lector.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            # Ve lo confirmado, no lo que el otro está escribiendo.
            assert vistos == 3

    with db.connect(ruta, readonly=True) as lector:
        assert lector.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 5


def test_en_solo_lectura_no_se_puede_escribir(tmp_path):
    """Si el dashboard pudiera escribir, un bug suyo corrompería la base que
    junta el scrape."""
    ruta = tmp_path / "t.db"
    with db.connect(ruta) as conn:
        poblar(conn, "Palermo", 1)

    with db.connect(ruta, readonly=True) as lector:
        with pytest.raises(sqlite3.OperationalError):
            lector.execute("DELETE FROM listings")
