"""Regresión del bug de mark_inactive.

Cuando Cloudflare cortaba una zona a mitad de camino, los avisos de esa zona
no llegaban a `seen_ids` y se daban de baja como si se hubieran vendido. Una
corrida bloqueada borraba medio barrio de la base. El arreglo fue pasarle a
`mark_inactive` solo las zonas que la fuente terminó de leer; este test está
para que no vuelva.
"""

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
