"""Cruce de venta contra alquiler del mismo edificio.

Lo que decide si esto sirve o miente es cómo se define "el mismo edificio".
Se usa el punto exacto al que geocodifica la dirección, y nada más: con
10.000 avisos en diez barrios, dos departamentos a 40 metros son vecinos
casi con seguridad, no la misma torre.
"""

from inmobot import db, yields


def poner(conn, avisos):
    db.upsert_listings(conn, [
        {
            "id": f"x:{i}", "source": "x", "source_id": str(i), "zone": "Palermo",
            "url": f"https://x/{i}", "title": "depto", **a,
        }
        for i, a in enumerate(avisos)
    ], keep_snapshots=False)


TORRE = {"latitude": -34.5812345, "longitude": -58.4312345, "address": "Gascón 300"}
VECINO = {"latitude": -34.5813000, "longitude": -58.4313000, "address": "Gascón 350"}


def test_rendimiento_de_un_edificio(tmp_path):
    """150.000 USD por 50 m² (3.000 el m²) con un alquiler de 600 por mes
    (12 el m²) son 4,8% anual: 12 x 12 / 3000."""
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**TORRE, "price_norm": 150_000, "covered_area": 50, "rooms": 2}])
        poner(a, [{**TORRE, "price_norm": 600, "covered_area": 50, "rooms": 2}])

        r = yields.rental_yields(v, a)
        assert len(r) == 1
        assert round(r.iloc[0]["rendimiento_pct"], 1) == 4.8
        assert round(r.iloc[0]["años_para_pagarlo"], 0) == 21


def test_un_vecino_no_es_el_mismo_edificio(tmp_path):
    """Gascón 350 está a media cuadra de Gascón 300. Si se emparejaran por
    cercanía, el alquiler de uno mediría el precio del otro."""
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**TORRE, "price_norm": 150_000, "covered_area": 50}])
        poner(a, [{**VECINO, "price_norm": 600, "covered_area": 50}])
        assert yields.rental_yields(v, a).empty


def test_compara_por_m2_y_no_por_tipologia(tmp_path):
    """Casi nunca hay venta y alquiler de la misma cantidad de ambientes en
    el mismo edificio. Midiendo por m², un monoambiente en alquiler sirve
    para medir un 3 ambientes en venta."""
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**TORRE, "price_norm": 300_000, "covered_area": 100, "rooms": 4}])
        poner(a, [{**TORRE, "price_norm": 300, "covered_area": 25, "rooms": 1}])

        r = yields.rental_yields(v, a)
        # venta 3.000/m², alquiler 12/m²: el mismo 4,8% aunque las
        # tipologías no tengan nada que ver.
        assert round(r.iloc[0]["rendimiento_pct"], 1) == 4.8


def test_sin_direccion_no_entra(tmp_path):
    """Remax y Mudafy publican coordenadas del portal, no calle y altura, y
    esas coordenadas son aproximadas. Un aviso sin dirección no puede
    afirmar que está en un edificio."""
    sin_direccion = {"latitude": TORRE["latitude"], "longitude": TORRE["longitude"],
                     "address": None}
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**sin_direccion, "price_norm": 150_000, "covered_area": 50}])
        poner(a, [{**TORRE, "price_norm": 600, "covered_area": 50}])
        assert yields.rental_yields(v, a).empty


def test_exigir_varios_avisos_de_cada_lado(tmp_path):
    """Un edificio con un aviso de cada lado da un número, pero es el
    capricho de dos publicaciones."""
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**TORRE, "price_norm": 150_000, "covered_area": 50}])
        poner(a, [{**TORRE, "price_norm": 600, "covered_area": 50}])

        assert len(yields.rental_yields(v, a, min_avisos=1)) == 1
        assert yields.rental_yields(v, a, min_avisos=2).empty


def test_sin_alquileres_devuelve_vacio(tmp_path):
    with db.connect(tmp_path / "v.db") as v, db.connect(tmp_path / "a.db") as a:
        poner(v, [{**TORRE, "price_norm": 150_000, "covered_area": 50}])
        assert yields.rental_yields(v, a).empty
