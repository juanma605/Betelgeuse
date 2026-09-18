"""Contrato de las fuentes: cada `_map()` contra una tarjeta real del portal.

Es el test que más vale del proyecto. Las cuatro fuentes scrapeadas dependen
de clases CSS y atributos `data-*` que los portales cambian sin avisar: cuando
eso pasa, el scraper no explota — sigue corriendo y llena la base de nulls.
Acá se pone en rojo el día que pasa, contra un fixture que no toca la red.

Ya sirvió una vez: ver `test_argenprop_map`.
"""

import json

from fake_dom import FIXTURES, card_from

from inmobot import db
from inmobot.sources import argenprop, mercadolibre, mudafy, remax, zonaprop
from inmobot.sources._text import parse_number

# `address` no está en el esquema (db.upsert_listings lo descarta) pero dos
# fuentes lo capturan. Se permite acá para no fingir que es parte del contrato.
SCHEMA_FIELDS = set(db.UPSERT_FIELDS) | {"address"}

NUMERIC_FIELDS = [
    "price", "maintenance_fee", "covered_area", "total_area", "rooms",
    "bedrooms", "bathrooms", "age_years", "photo_count", "latitude", "longitude",
]


def assert_esquema_comun(item: dict) -> None:
    sobrantes = set(item) - SCHEMA_FIELDS
    assert not sobrantes, f"claves que la base va a tirar: {sobrantes}"
    assert item["id"] == f"{item['source']}:{item['source_id']}"
    assert item["url"].startswith("https://")
    assert item["title"]
    for field in NUMERIC_FIELDS:
        value = item.get(field)
        assert value is None or isinstance(value, (int, float)), f"{field}={value!r}"


def test_parse_number_distingue_miles_de_decimales():
    assert parse_number("$ 200.000 Expensas") == 200_000       # miles, a la argentina
    assert parse_number("1.234,5") == 1234.5
    assert parse_number("174.37 m²") == 174.37                 # no es grupo de 3: decimal
    # "33.420" es ambiguo de verdad, y ahí manda quién lo escribió: para
    # Remax son 33,42 m². Sin esto entraba un depto de 33.420 m² a la base.
    assert parse_number("33.420") == 33_420
    assert parse_number("33.420", decimal_point=True) == 33.42


def test_zonaprop_map():
    card = card_from("zonaprop_card.html", "[data-posting-type]")
    item = zonaprop.build({})._map(card, "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "zonaprop:12345678"
    assert (item["price"], item["currency"]) == (145_000, "USD")
    assert (item["neighborhood"], item["city"]) == ("Almagro", "Capital Federal")
    assert (item["total_area"], item["rooms"]) == (72, 3)
    assert (item["bedrooms"], item["bathrooms"]) == (2, 2)
    assert item["maintenance_fee"] == 200_000
    assert item["photo_count"] == 1
    # Zonaprop no publica un título corto en la tarjeta: se usa el arranque de
    # la descripción, recortado.
    assert item["title"].startswith("Departamento de tres ambientes")
    assert len(item["title"]) <= 121


def test_argenprop_map():
    """Argenprop sacó `.card__title--primary` de sus tarjetas (visto el
    2026-09-18) y con eso se llevó barrio y ciudad. El barrio ahora sale de
    la dirección; la ciudad se perdió y queda en None a propósito, antes que
    inventarla."""
    card = card_from("argenprop_card.html", "a.card")
    item = argenprop.build({})._map(card, "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "argenprop:20431805"
    assert (item["price"], item["currency"]) == (125_000, "USD")
    assert item["neighborhood"] == "Almagro Sur"
    assert item["city"] is None
    assert (item["covered_area"], item["rooms"]) == (80, 2)
    assert (item["bedrooms"], item["bathrooms"], item["age_years"]) == (1, 1, 25)
    assert item["maintenance_fee"] == 390_000
    # El contador dice "1/20": es la foto actual, no el total. Alcanza para
    # el filtro require_photos, que solo mira que haya alguna.
    assert item["photo_count"] == 1


def test_mudafy_map():
    card = card_from("mudafy_card.html", 'a[href^="/departamentos/"]:has(h3)')
    item = mudafy.build({})._map(card, "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "mudafy:680464"
    assert (item["price"], item["currency"]) == (64_900, "USD")
    assert (item["neighborhood"], item["city"]) == ("Almagro", "CABA")
    assert (item["total_area"], item["rooms"]) == (36, 2)
    assert (item["bedrooms"], item["bathrooms"]) == (1, 1)
    assert item["maintenance_fee"] == 130_000
    assert item["title"] == "Don Bosco 3825"
    # La foto vive en el carrusel hermano del <a>, no adentro.
    assert item["photo_count"] == 1


def test_remax_map():
    card = card_from("remax_card.html", ".card-remax")
    item = remax.build({})._map(card, "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "remax:venta-departamento-dos-ambientes-con-balcon"
    assert (item["price"], item["currency"]) == (75_000, "USD")
    # Remax publica los m² con decimales: el punto es decimal, no de miles.
    assert item["total_area"] == 39.04
    assert item["covered_area"] == 25.5
    assert (item["rooms"], item["bathrooms"]) == (2, 1)
    assert item["maintenance_fee"] == 75_000
    assert item["photo_count"] == 3


def test_remax_saca_las_coordenadas_del_estado_de_la_pagina():
    state = (FIXTURES / "remax_state.json").read_text(encoding="utf-8")
    coords = remax.coords_by_slug(state)

    # GeoJSON viene [lon, lat]: si se dieran vuelta, los puntos caerían en
    # el océano Antártico y nadie lo notaría hasta abrir el mapa.
    assert coords["venta-departamento-dos-ambientes-con-balcon"] == (-34.6102, -58.4201)
    assert "venta-monoambiente-sin-ubicacion" not in coords
    assert len(coords) == 2
    assert remax.coords_by_slug("") == {}


def test_mudafy_no_le_presta_coordenadas_a_un_aviso_que_no_las_tiene():
    html = (FIXTURES / "mudafy_payload.html").read_text(encoding="utf-8")
    coords = mudafy.coords_by_id(html)

    # Indexado por el mismo id que usa _map() (el sufijo numérico del slug).
    assert coords["680464"] == (-34.614, -58.419)
    assert coords["201734"] == (-34.617, -58.416)
    # El del medio no trae coordenadas: no puede quedarse con las del
    # siguiente aviso ni con el `barycenter` del barrio.
    assert "157315" not in coords


def test_mercadolibre_map():
    """El fixture está armado a mano (la API responde 403 sin certificación),
    así que fija el mapeo de `attributes`, no que ML siga contestando igual."""
    raw = json.loads((FIXTURES / "mercadolibre_search.json").read_text(encoding="utf-8"))
    source = mercadolibre.build({"site": "MLA", "category": "MLA1474"})
    item = source._map(raw["results"][0], "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "mercadolibre:MLA1234567890"
    assert (item["price"], item["currency"]) == (145_000, "USD")
    assert (item["neighborhood"], item["city"]) == ("Almagro", "Capital Federal")
    assert (item["covered_area"], item["total_area"]) == (72.0, 78.0)
    assert (item["rooms"], item["bedrooms"], item["bathrooms"]) == (3, 2, 2)
    assert (item["maintenance_fee"], item["age_years"]) == (200_000.0, 25)
    assert item["latitude"] == -34.606543
    assert item["photo_count"] == 2
