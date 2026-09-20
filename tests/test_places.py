"""Distancias a subtes, hospitales y comisarías, y el puntaje de ubicación."""

import numpy as np
import pytest

from inmobot import places

OBELISCO = (-34.60373, -58.38159)
PLAZA_DE_MAYO = (-34.60836, -58.37232)  # ~1 km al sudeste del Obelisco


def test_distancia_entre_dos_puntos_conocidos():
    metros = places.distancia_m(*OBELISCO, *PLAZA_DE_MAYO)
    # Medido en el mapa: 1,0 km. La aproximación plana puede errar metros,
    # no cientos.
    assert 950 < metros < 1100


def test_distancia_al_mas_cercano_elige_el_mas_cercano_y_no_inventa():
    lugares = [
        {"nombre": "lejos", "lat": -34.55, "lon": -58.50},
        {"nombre": "cerca", "lat": PLAZA_DE_MAYO[0], "lon": PLAZA_DE_MAYO[1]},
    ]
    distancias = places.distancia_al_mas_cercano(
        [OBELISCO[0], np.nan], [OBELISCO[1], np.nan], lugares
    )
    assert 950 < distancias[0] < 1100
    # Un aviso sin coordenadas no tiene distancia: NaN, no cero (cero sería
    # "pegado al subte", el mejor puntaje posible).
    assert np.isnan(distancias[1])


def test_puntaje_ubicacion_premia_el_subte_y_castiga_estar_pegado():
    subte = np.array([0.0, 500.0, 1000.0, 2000.0, 100.0])
    evitar = np.array([1000.0, 1000.0, 1000.0, 1000.0, 50.0])
    puntos = places.puntaje_ubicacion(subte, evitar, subte_m=1000, evitar_m=200)

    assert puntos[0] == pytest.approx(1.0)    # en la puerta del subte y tranquilo
    assert puntos[1] == pytest.approx(0.65)   # a mitad de camino
    assert puntos[2] == pytest.approx(0.3)    # justo en el límite: solo tranquilidad
    assert puntos[3] == pytest.approx(0.3)    # más lejos no resta de más
    # A 100 m del subte pero con una comisaría a 50 m: pierde la tranquilidad.
    assert puntos[4] == pytest.approx(0.7 * 0.9)


def test_lugares_json_tiene_las_tres_listas_dentro_de_caba():
    lugares = places.cargar()
    for clave in ("subte", "hospitales", "comisarias"):
        assert lugares[clave], f"{clave} vacío"
        for lugar in lugares[clave]:
            assert -34.71 < lugar["lat"] < -34.52, lugar
            assert -58.54 < lugar["lon"] < -58.33, lugar
