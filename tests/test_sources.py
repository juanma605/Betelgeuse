"""Contrato de las fuentes: cada `_map()` contra una tarjeta real del portal.

Es el test que más vale del proyecto. Las cuatro fuentes scrapeadas dependen
de clases CSS y atributos `data-*` que los portales cambian sin avisar: cuando
eso pasa, el scraper no explota — sigue corriendo y llena la base de nulls.
Acá se pone en rojo el día que pasa, contra un fixture que no toca la red.

Ya sirvió una vez: ver `test_argenprop_map`.
"""

from fake_dom import FIXTURES, card_from

from inmobot import db
from inmobot.sources import argenprop, mercadolibre, mudafy, remax, zonaprop
from inmobot.sources._browser import is_bot_challenge
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
    # Zonaprop tampoco publica coordenadas, pero sí calle y altura.
    assert item["address"] == "Gascon al 300"


def test_zonaprop_no_confunde_el_titulo_con_la_direccion():
    # Cuando el aviso no tiene dirección, Zonaprop mete el título en el mismo
    # elemento. Un título es largo; una dirección, no.
    assert zonaprop._direccion("Muñiz 778") == "Muñiz 778"
    assert zonaprop._direccion(
        "Departamento en Almagro en Venta I 2 Ambientes con Balcón, Vestidor y Amenities"
    ) is None


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


def test_los_ordenes_extra_salen_de_lo_que_cada_robots_txt_habilita():
    """Zonaprop y Argenprop prohíben reordenar la búsqueda salvo por precio
    ascendente, que habilitan con un Allow puntual. Estas URLs son las que
    caen del lado permitido: si alguien agrega otro orden al config, que sea
    a sabiendas y no porque el código lo arma solo."""
    zp = zonaprop.build({"extra_orders": ["-orden-precio-ascendente"]})
    assert zp._url("departamentos", "Palermo", 1, "-orden-precio-ascendente") == (
        "https://www.zonaprop.com.ar/departamentos-venta-palermo-orden-precio-ascendente.html"
    )

    # Argenprop no permite combinar orden con paginación (`Disallow: /*?*&*`),
    # así que el orden nunca lleva `&pagina-N` pegado.
    ap = argenprop.build({"extra_orders": ["orden-menorprecio"]})
    con_orden = ap._url("departamentos", "Palermo", 2, "orden-menorprecio")
    assert con_orden == "https://www.argenprop.com/departamentos/venta/palermo?orden-menorprecio"
    assert "&" not in con_orden

    # Sin configurar, las URLs son las de siempre.
    assert zonaprop.build({})._url("departamentos", "Palermo", 2).endswith("-pagina-2.html")
    assert argenprop.build({})._url("departamentos", "Palermo", 2).endswith("?pagina-2")


def test_remax_le_pega_la_provincia_al_barrio():
    """Remax no responde 404 ante un slug que no reconoce: devuelve otra
    búsqueda. `-en-palermo` es un landing residual de 1 aviso y
    `-en-villa-urquiza` da 0, mientras los de verdad tienen 1466 y 452. La
    base tuvo un solo aviso de Palermo durante semanas sin un solo error."""
    source = remax.build({"zone_suffix": "-capital-federal"})

    assert source._url("Palermo", 1) == (
        "https://www.remax.com.ar/departamentos-en-venta-en-palermo-capital-federal"
    )
    # El sufijo va pegado al barrio, antes del `?page=` (0-indexado).
    assert source._url("Villa Urquiza", 2) == (
        "https://www.remax.com.ar/departamentos-en-venta-en-villa-urquiza-capital-federal?page=1"
    )
    # Sin configurar, la URL es la de antes: buscar fuera de CABA no necesita
    # sufijo y esto no se lo impone.
    assert remax.build({})._url("Palermo", 1).endswith("-en-palermo")


class _PaginaFalsa:
    def __init__(self, texto): self._texto = texto
    def inner_text(self, _sel): return self._texto


def test_remax_distingue_el_final_de_la_lista_de_una_carga_fallida():
    """Al pasarse de la última página, Remax no da 404: muestra "No hay
    propiedades que coincidan" y el selector de tarjetas nunca aparece, o
    sea la misma excepción que una carga fallida. Confundirlos marca la zona
    como incompleta y sus avisos vendidos no se dan de baja nunca."""
    source = remax.build({})
    assert source._sin_resultados(_PaginaFalsa("No hay propiedades que coincidan con tu búsqueda"))
    assert not source._sin_resultados(_PaginaFalsa("Verificación de seguridad en curso"))

    # Si la página ni siquiera se deja leer, es una falla, no un final.
    class Rota:
        def inner_text(self, _sel): raise RuntimeError("sin página")
    assert not source._sin_resultados(Rota())


def test_remax_se_da_cuenta_cuando_le_devuelven_otra_busqueda():
    """El fallo que nos metió 148 avisos de Allen, San Jerónimo y Mar del
    Plata en la base: Remax no responde 404 ante un barrio que no conoce
    (Las Cañitas no existe para ellos), devuelve otra búsqueda. Los avisos
    son válidos —precio, m², fotos— y lo único que los delata es el barrio."""
    palermo = ["Palermo, Capital Federal", "Palermo Chico, Capital Federal"]
    assert not remax.busqueda_degradada(palermo, "Palermo")
    # Buscando Cañitas volvieron los 22.864 del país entero.
    del_pais = ["Caballito, Capital Federal", "Balvanera, Capital Federal", "Allen, Río Negro"]
    assert remax.busqueda_degradada(del_pais, "Cañitas")
    # Esos mismos resultados son legítimos si lo que pedimos era Caballito.
    assert not remax.busqueda_degradada(del_pais, "Caballito")
    # Sin etiquetas no se acusa: si Remax cambia el state, el scraper sigue
    # trayendo avisos en vez de cortar todas las zonas.
    assert not remax.busqueda_degradada([], "Palermo")


def test_remax_lee_los_barrios_del_estado_de_la_pagina():
    state = (FIXTURES / "remax_state.json").read_text(encoding="utf-8")
    assert sorted(set(remax.geo_labels(state))) == [
        "Almagro, Capital Federal", "Boedo, Capital Federal",
    ]
    # Una búsqueda de Almagro que devuelve Almagro y un vecino está bien;
    # lo que delata a la degradada es que el barrio pedido no esté en ninguno.
    assert not remax.busqueda_degradada(remax.geo_labels(state), "Almagro")
    assert remax.busqueda_degradada(remax.geo_labels(state), "Villa Urquiza")
    assert remax.geo_labels("") == []


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


def test_mercadolibre_descarta_publicidad_y_emprendimientos():
    html = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")
    items = mercadolibre.parse_listing_page(html, "Almagro")

    # Un emprendimiento es un edificio con precio "Desde" y rangos de
    # ambientes y m²: no hay un depto que comparar. La publicidad linkea a un
    # tracker, no al aviso.
    assert [i["id"] for i in items] == ["mercadolibre:MLA2000000002", "mercadolibre:MLA3000000003"]


def test_mercadolibre_map():
    html = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")
    item = mercadolibre.parse_listing_page(html, "Almagro")[0]

    assert_esquema_comun(item)
    assert (item["price"], item["currency"]) == (145_000, "USD")  # ML escribe "US$"
    assert (item["neighborhood"], item["city"]) == ("Almagro", "Capital Federal")
    assert (item["rooms"], item["bathrooms"]) == (4, 2)
    assert item["photo_count"] == 1
    # Sin el "#polycard_client=..." de tracking.
    assert item["url"] == (
        "https://departamento.mercadolibre.com.ar/MLA-2000000002-venta-depto-4-ambientes-balcon-_JM"
    )


def test_mercadolibre_no_confunde_m2_totales_con_cubiertos():
    html = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")
    cubierto, sin_calificar = mercadolibre.parse_listing_page(html, "Almagro")

    assert (cubierto["covered_area"], cubierto.get("total_area")) == (81, None)
    # "70 m²" a secas no se toma como cubierto: va a total_area, así
    # load_active lo marca como área estimada y no entra a las medianas.
    assert (sin_calificar.get("covered_area"), sin_calificar["total_area"]) == (None, 70)


def test_reconoce_el_cartel_de_cloudflare_en_los_dos_idiomas():
    """Cloudflare le sirve a Argenprop la pantalla en inglés, y por eso sus
    bloqueos venían apareciendo en el log como `Timeout 10000ms exceeded`.
    Un scraper que parece lento cuando en realidad lo están frenando es la
    peor forma de fallar: se arregla lo que no está roto."""
    class Pagina:
        def __init__(self, titulo, cuerpo): self._t, self._c = titulo, cuerpo
        def title(self): return self._t
        def inner_text(self, _sel): return self._c

    assert is_bot_challenge(Pagina("Un momento...", "Verificación de seguridad en curso"))
    assert is_bot_challenge(Pagina("Just a moment...", "Let's confirm you are human"))
    assert not is_bot_challenge(Pagina("Departamentos en venta en Palermo", "20 resultados"))
