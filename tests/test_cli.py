"""Cómo se orquestan las fuentes en una corrida.

Los portales se recorren en paralelo, uno por hilo. Eso no cambia el ritmo
con que se le pide a cada sitio —cada fuente mantiene su rate limit— pero sí
mete dos riesgos nuevos: que una fuente rota arrastre a las demás, y que
varios hilos escriban la base al mismo tiempo.
"""

import sqlite3

from inmobot import cli


class FuenteFalsa:
    def __init__(self, avisos, explota=False):
        self._avisos, self._explota = avisos, explota
        self.incomplete_zones: set[str] = set()
        self.capped_zones: set[str] = set()

    def fetch(self, zone, search_cfg):
        if self._explota:
            raise RuntimeError("el portal cambió el HTML")
        for i in range(self._avisos):
            yield {
                "id": f"x:{zone}-{i}", "source": "x", "source_id": str(i),
                "url": f"https://x/{zone}/{i}", "title": "depto", "zone": zone,
                "price": 150_000, "currency": "USD", "covered_area": 50,
            }


SEARCH = {
    "zones": ["Palermo", "Almagro"],
    "price_min": 10_000, "price_max": 600_000,
    "fx_rates": {"ARS_per_USD": 1500}, "currency": "USD",
    "filters": {},
}


def test_recolectar_junta_todas_las_zonas_y_no_toca_la_base():
    source, kept, seen, rejected = cli._recolectar(
        "x", FuenteFalsa(avisos=3), SEARCH, {"enabled": False}
    )
    assert len(kept) == 6          # 3 avisos x 2 zonas
    assert len(seen) == 6
    assert rejected == 0
    assert source.incomplete_zones == set()


def test_recolectar_cuenta_los_que_no_pasan_los_filtros():
    exigente = {**SEARCH, "filters": {"covered_area_min": 80}}
    _, kept, seen, rejected = cli._recolectar("x", FuenteFalsa(2), exigente, {"enabled": False})
    assert (kept, seen) == ([], set())
    assert rejected == 4


def test_una_fuente_rota_no_se_lleva_puestas_a_las_demas(tmp_path, monkeypatch, caplog):
    """Con las fuentes en paralelo, una excepción sin atajar mataría la
    corrida entera en vez de un solo portal."""
    from inmobot.config import Config

    monkeypatch.setitem(cli.SOURCE_BUILDERS, "rota", lambda conf: FuenteFalsa(0, explota=True))
    monkeypatch.setitem(cli.SOURCE_BUILDERS, "sana", lambda conf: FuenteFalsa(2))

    cfg = Config({
        "search": SEARCH,
        "dedup": {"enabled": False},
        "sources": {"rota": {"enabled": True}, "sana": {"enabled": True}},
        "storage": {"path": str(tmp_path / "t.db"), "keep_snapshots": False},
        "scrape": {"parallel_sources": 2},
        "geocoding": {"enabled": False},
    })
    cli.cmd_scrape(cfg)

    conn = sqlite3.connect(tmp_path / "t.db")
    guardados = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert guardados == 4, "los avisos de la fuente sana tienen que haber entrado"
    assert "la fuente falló" in caplog.text
