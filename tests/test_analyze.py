"""Análisis, contra datos plantados a mano donde la respuesta se sabe de antemano."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from inmobot import analyze, db

CFG = {
    "min_comparables": 5,
    "outlier_trim_pct": 0,
    "undervalued_threshold_pct": 15,
    "stale_days": 60,
}

RECIENTE = datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def frame(rows: list[dict]) -> pd.DataFrame:
    """Completa las columnas derivadas que normalmente arma load_active()."""
    df = pd.DataFrame(rows)
    df["first_seen"] = df["first_seen"].fillna(RECIENTE) if "first_seen" in df else RECIENTE
    df["off_plan"] = (
        df["off_plan"].fillna(False).astype(bool) if "off_plan" in df else False
    )
    df["price_per_m2"] = df["price_norm"] / df["area"]
    return df


def mercado(n: int = 10, zone: str = "Almagro", precio: float = 100_000) -> list[dict]:
    """n avisos idénticos de 50 m²: la mediana del grupo es precio/50 por m²."""
    return [
        {"id": f"base:{i}", "zone": zone, "rooms": 2, "area": 50, "price_norm": precio}
        for i in range(n)
    ]


# --- find_undervalued -------------------------------------------------- #

def test_find_undervalued_encuentra_el_aviso_plantado_30_abajo():
    rows = mercado() + [
        {"id": "ganga", "zone": "Almagro", "rooms": 2, "area": 50, "price_norm": 70_000}
    ]
    hits = analyze.find_undervalued(frame(rows), CFG)

    assert list(hits["id"]) == ["ganga"]
    assert hits.iloc[0]["discount_pct"] == pytest.approx(30.0)


def test_find_undervalued_ignora_grupos_con_pocos_comparables():
    # 3 avisos no son un mercado: sin min_comparables, una "mediana" de tres
    # publicaciones convertiría cualquier outlier en oportunidad.
    rows = mercado(n=3) + [
        {"id": "ganga", "zone": "Almagro", "rooms": 2, "area": 50, "price_norm": 70_000}
    ]
    assert analyze.find_undervalued(frame(rows), CFG).empty


def test_zone_stats_excluye_los_avisos_de_pozo():
    rows = mercado() + [
        {"id": "pozo", "zone": "Almagro", "rooms": 2, "area": 50,
         "price_norm": 50_000, "off_plan": True}
    ]
    stats = analyze.zone_stats(frame(rows), CFG)

    assert stats.iloc[0]["n"] == 10
    assert stats.iloc[0]["median"] == pytest.approx(2000.0)


def test_zone_stats_excluye_los_avisos_viejos_de_la_mediana():
    viejo = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="milliseconds")
    rows = mercado() + [
        {"id": "viejo", "zone": "Almagro", "rooms": 2, "area": 50,
         "price_norm": 40_000, "first_seen": viejo}
    ]
    stats = analyze.zone_stats(frame(rows), CFG)

    assert stats.iloc[0]["n"] == 10


def test_zone_stats_recorta_los_extremos():
    rows = mercado(n=8) + [
        {"id": "regalado", "zone": "Almagro", "rooms": 2, "area": 50, "price_norm": 5_000},
        {"id": "delirante", "zone": "Almagro", "rooms": 2, "area": 50, "price_norm": 500_000},
    ]
    stats = analyze.zone_stats(frame(rows), {**CFG, "outlier_trim_pct": 10})

    assert stats.iloc[0]["n"] == 8
    assert stats.iloc[0]["median"] == pytest.approx(2000.0)


def test_is_off_plan_reconoce_el_pozo_por_la_url():
    # Los emprendimientos de Zonaprop tienen títulos de marketing que no dicen
    # ni "pozo" ni "emprendimiento", pero el portal los cuelga de /emprendimiento/.
    assert analyze.is_off_plan("Folium Jufre: Emplazado en Jufre 975", None) is False
    assert analyze.is_off_plan(
        "Folium Jufre: Emplazado en Jufre 975",
        "https://www.zonaprop.com.ar/propiedades/emprendimiento/folium-jufre-58654141.html",
    ) is True


def test_un_aviso_con_solo_m2_totales_no_mueve_la_mediana_y_sale_marcado(tmp_path):
    # El caso real que motivó esto: un "2 amb de 138 m²" que no publica m²
    # cubiertos aparecía primero entre los subvaluados con 61% de descuento.
    # Casi seguro un PH con patio: el área total infla el denominador.
    avisos = [
        {"id": f"remax:{i}", "source": "remax", "source_id": str(i), "title": "Depto",
         "zone": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 100_000}
        for i in range(10)
    ] + [
        {"id": "zonaprop:ph", "source": "zonaprop", "source_id": "ph", "title": "PH",
         "zone": "Almagro", "rooms": 2, "covered_area": None, "total_area": 138,
         "price_norm": 119_000},
    ]
    with db.connect(tmp_path / "t.db") as conn:
        db.upsert_listings(conn, avisos)
        df = analyze.load_active(conn)

    stats = analyze.zone_stats(df, CFG)
    assert stats.iloc[0]["n"] == 10
    assert stats.iloc[0]["median"] == pytest.approx(2000.0)

    hits = analyze.find_undervalued(df, CFG).set_index("id")
    assert list(hits.index) == ["zonaprop:ph"]
    assert bool(hits.loc["zonaprop:ph", "area_estimada"]) is True


def test_comparables_note_explica_por_que_no_hay_estadisticas():
    note = analyze.comparables_note(frame(mercado(n=3)), CFG)

    assert "min_comparables=5" in note
    assert "3 avisos" in note


# --- price_drops ------------------------------------------------------- #

def snapshots(conn, filas):
    conn.executemany(
        "INSERT INTO price_snapshots (listing_id, seen_at, price, currency, price_norm) "
        "VALUES (?, ?, ?, ?, ?)",
        filas,
    )


def test_price_drops_detecta_la_baja_y_calcula_el_porcentaje(tmp_path):
    with db.connect(tmp_path / "t.db") as conn:
        snapshots(conn, [
            ("a", "2026-01-01T00:00:00.000+00:00", 100_000, "USD", 100_000),
            ("a", "2026-02-01T00:00:00.000+00:00", 90_000, "USD", 90_000),
        ])
        drops = analyze.price_drops(conn)

    assert len(drops) == 1
    assert drops.iloc[0]["drops"] == 1
    assert drops.iloc[0]["total_drop_pct"] == pytest.approx(10.0)


def test_price_drops_con_una_sola_corrida_devuelve_vacio_sin_romper(tmp_path):
    # Es el estado de cualquier instalación nueva: no es un error.
    with db.connect(tmp_path / "t.db") as conn:
        snapshots(conn, [
            ("a", "2026-01-01T00:00:00.000+00:00", 100_000, "USD", 100_000),
            ("b", "2026-01-01T00:00:00.000+00:00", 120_000, "USD", 120_000),
        ])
        drops = analyze.price_drops(conn)

    assert drops.empty


def test_history_note_avisa_cuando_falta_una_segunda_corrida(tmp_path):
    with db.connect(tmp_path / "t.db") as conn:
        snapshots(conn, [("a", "2026-01-01T00:00:00.000+00:00", 100_000, "USD", 100_000)])
        assert "2 corridas" in analyze.history_note(conn)


def test_history_note_calla_cuando_ya_hay_historial(tmp_path):
    with db.connect(tmp_path / "t.db") as conn:
        snapshots(conn, [
            ("a", "2026-01-01T00:00:00.000+00:00", 100_000, "USD", 100_000),
            ("a", "2026-02-01T00:00:00.000+00:00", 100_000, "USD", 100_000),
        ])
        assert analyze.history_note(conn) is None
