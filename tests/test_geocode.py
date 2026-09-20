"""Geocodificación de las direcciones, sin tocar la red."""

import httpx
import pytest

from inmobot import db, geocode


def cliente_falso(respuestas: list, llamadas: list | None = None) -> httpx.Client:
    """Devuelve las respuestas dadas, en orden, y anota qué se preguntó."""
    pendientes = list(respuestas)

    def responder(request: httpx.Request) -> httpx.Response:
        if llamadas is not None:
            llamadas.append(request.url.params.get("direccion"))
        return httpx.Response(200, json=pendientes.pop(0))

    return httpx.Client(transport=httpx.MockTransport(responder))


def normalizada(lat: float, lon: float, cantidad: int = 1) -> dict:
    return {
        "direccionesNormalizadas": [
            {"altura": 3700, "nombre_calle": "BELGRANO AV.",
             "coordenadas": {"srid": 4326, "x": str(lon), "y": str(lat)}}
        ] * cantidad
    }


@pytest.mark.parametrize("texto, esperado", [
    ("Mexico 4200, Piso 3, Almagro Sur", "Mexico 4200"),
    ("Avenida Belgrano 3700, Almagro, Capital Federal", "Avenida Belgrano 3700"),
    ("Av. Corrientes 3401", "Av Corrientes 3401"),
    # MercadoLibre mete "al" (de altura) antes del número y con eso el
    # normalizador contesta "calle inexistente".
    ("Jeronimo Salguero Al 2300", "Jeronimo Salguero 2300"),
    ("Gallo altura 1200", "Gallo 1200"),
    ("Olazabal Al 2600 - Piso 2", "Olazabal 2600"),
    ("Zapata y Matienzo - Unidad 703", None),  # esquina, no calle y altura
    # Sin altura no se geocodifica: una calle tiene treinta cuadras y el
    # punto caería en cualquier lado.
    ("Bulnes, Almagro", None),
    (None, None),
])
def test_limpiar_direccion(texto, esperado):
    assert geocode.limpiar_direccion(texto) == esperado


def test_geocodificar_devuelve_lat_lon():
    assert geocode.geocodificar(
        "Avenida Belgrano 3700", cliente_falso([normalizada(-34.615849, -58.418280)])
    ) == (-34.615849, -58.418280)


def test_geocodificar_descarta_lo_ambiguo_y_lo_que_cae_fuera_de_caba():
    # Dos opciones: el normalizador no sabe cuál es. Mejor sin ubicación que
    # en la cuadra equivocada.
    assert geocode.geocodificar(
        "Salguero 2300", cliente_falso([normalizada(-34.58, -58.41, cantidad=2)])
    ) is None
    # Una dirección de otra ciudad no puede terminar en el mapa de CABA.
    assert geocode.geocodificar(
        "Belgrano 3700", cliente_falso([normalizada(-31.42, -64.18)])
    ) is None
    assert geocode.geocodificar(
        "Belgrano 3700", cliente_falso([{"direccionesNormalizadas": []}])
    ) is None


def test_completar_coordenadas_ubica_cachea_y_no_repregunta(tmp_path):
    avisos = [
        {"id": "ml:1", "source": "mercadolibre", "source_id": "1", "zone": "Almagro",
         "address": "Avenida Belgrano 3700, Almagro", "price_norm": 100_000},
        # Misma dirección en otro portal: no puede gastar una segunda consulta.
        {"id": "ap:2", "source": "argenprop", "source_id": "2", "zone": "Almagro",
         "address": "Avenida Belgrano 3700, Piso 2", "price_norm": 110_000},
        {"id": "ml:3", "source": "mercadolibre", "source_id": "3", "zone": "Almagro",
         "address": "Bulnes, Almagro", "price_norm": 90_000},  # sin altura
    ]
    llamadas: list = []
    with db.connect(tmp_path / "t.db") as conn:
        db.upsert_listings(conn, avisos)
        stats = geocode.completar_coordenadas(
            conn, {"rate_limit_seconds": 0},
            cliente_falso([normalizada(-34.615849, -58.418280)], llamadas),
        )
        ubicados = dict(conn.execute("SELECT id, latitude FROM listings").fetchall())
        cache = conn.execute("SELECT address, lat FROM geocode_cache").fetchall()

    assert llamadas == ["Avenida Belgrano 3700, CABA"]  # una sola consulta
    assert stats == {"ubicados": 2, "consultadas": 1, "sin_resultado": 0, "pendientes": 1}
    assert ubicados["ml:1"] == ubicados["ap:2"] == -34.615849
    assert ubicados["ml:3"] is None
    assert [tuple(f) for f in cache] == [("Avenida Belgrano 3700", -34.615849)]
