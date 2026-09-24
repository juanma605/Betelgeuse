"""Contrato de las fuentes: cada `_map()` contra una tarjeta real del portal.

Es el test que más vale del proyecto. Las cuatro fuentes scrapeadas dependen
de clases CSS y atributos `data-*` que los portales cambian sin avisar: cuando
eso pasa, el scraper no explota — sigue corriendo y llena la base de nulls.
Acá se pone en rojo el día que pasa, contra un fixture que no toca la red.

Ya sirvió una vez: ver `test_argenprop_map`.
"""

from datetime import date

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


def test_zonaprop_no_toma_un_emprendimiento_por_un_departamento():
    """El listado de Zonaprop mezcla avisos sueltos con edificios enteros, y
    el edificio publica el precio de la unidad más chica junto al rango de
    superficies: "desde USD 148.680" y "48 a 148 m² tot.". El parser se
    quedaba con el precio de la unidad de 48 m² y los m² de la de 148, o sea
    1.005 USD/m² en Palermo, donde la mediana ronda los 2.700. Un 66% de
    descuento fabricado por nosotros, entrando justo a la lista de
    subvaluados, que es la salida principal del proyecto."""
    card = card_from("zonaprop_development_card.html", "[data-posting-type]")
    assert zonaprop.build({})._map(card, "Palermo") is None

    # El aviso suelto de al lado sigue entrando.
    suelto = card_from("zonaprop_card.html", "[data-posting-type]")
    assert zonaprop.build({})._map(suelto, "Almagro") is not None


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


def test_mudafy_ficha():
    html = (FIXTURES / "mudafy_ficha.html").read_text(encoding="utf-8")
    url = "https://mudafy.com.ar/departamentos/calle-falsa-100-departamento-en-venta-500001"
    item = mudafy.parse_ficha(html, url)

    assert_esquema_comun(item)
    assert item["id"] == "mudafy:500001"
    assert (item["price"], item["currency"]) == (123_000, "USD")
    assert (item["neighborhood"], item["city"]) == ("Caballito", "CABA")
    assert (item["latitude"], item["longitude"]) == (-34.62, -58.44)
    assert (item["rooms"], item["bedrooms"], item["bathrooms"]) == (3, 2, 1)
    # Lo que la tarjeta no traía. Son del aviso propio, no de los
    # "similares" que la página muestra antes y después (250 m², 1950,
    # 999.000 de expensas): esos son los que agarraría leer el primero
    # que aparece.
    assert (item["covered_area"], item["total_area"]) == (64, 70)
    assert item["maintenance_fee"] == 150_000
    assert item["age_years"] == date.today().year - 2000
    # Dos fotos de esta publicación; la tercera es de otra.
    assert item["photo_count"] == 2

    # Casos reales: expensas sin moneda (la tarjeta las muestra en pesos) y
    # cubierta mayor que total por un error de tipeo del anunciante.
    raro = html.replace('"amount\\":150000,\\"currency\\":\\"ARS\\"', '"amount\\":150000,\\"currency\\":null')
    raro = raro.replace('"roofed_area\\":64', '"roofed_area\\":640')
    assert raro != html
    item = mudafy.parse_ficha(raro, url)
    assert item["maintenance_fee"] == 150_000
    assert (item["covered_area"], item["total_area"]) == (None, 70)


def test_mudafy_ficha_vendida_o_redirigida_no_es_un_aviso():
    html = (FIXTURES / "mudafy_ficha.html").read_text(encoding="utf-8")
    vendida = html.replace("schema.org/InStock", "schema.org/SoldOut")
    url = "https://mudafy.com.ar/departamentos/calle-falsa-100-departamento-en-venta-500001"
    assert mudafy.parse_ficha(vendida, url) is None
    assert mudafy.parse_ficha(html, "https://mudafy.com.ar/venta/departamentos") is None


def test_mudafy_filtra_el_sitemap_por_tipo_y_operacion():
    fuente = mudafy.build({"property_slug": "departamentos", "operation_slug": "venta"})
    assert fuente._es_del_tipo("https://mudafy.com.ar/departamentos/x-100-departamento-en-venta-1")
    assert not fuente._es_del_tipo("https://mudafy.com.ar/departamentos/x-100-departamento-en-alquiler-1")
    assert not fuente._es_del_tipo("https://mudafy.com.ar/casas/x-100-casa-en-venta-1")
    # Basura real del sitemap: sin id.
    assert not fuente._es_del_tipo("https://mudafy.com.ar/departamentos/aaa")


def test_mudafy_elige_primero_lo_nuevo_y_despues_lo_mas_viejo_de_nuestras_zonas():
    zonas = ["Palermo", "Caballito"]
    cache = {
        "10": {"url": "u10", "barrio": "Palermo Soho", "visto": "2026-09-20"},
        "11": {"url": "u11", "barrio": "Caballito", "visto": "2026-09-10"},
        # Leída y de otro lado: no se vuelve a pedir nunca.
        "12": {"url": "u12", "barrio": "Pilar", "visto": "2026-09-01"},
    }
    tarjetas = {"1": "u1", "10": "u10"}
    sitemap = {"2": "u2", "12": "u12", "1": "u1"}

    cola = mudafy.elegir_fichas(tarjetas, sitemap, cache, zonas, tope=10)
    assert [sid for sid, _ in cola] == ["1", "2", "11", "10"]
    # El tope corta por el final: lo que se posterga es el refresco.
    assert [sid for sid, _ in mudafy.elegir_fichas(tarjetas, sitemap, cache, zonas, 3)] == ["1", "2", "11"]



def test_los_ordenes_extra_salen_de_lo_que_cada_robots_txt_habilita():
    """Zonaprop y Argenprop prohíben reordenar la búsqueda salvo por precio
    ascendente, que habilitan con un Allow puntual. Estas URLs son las que
    caen del lado permitido: si alguien agrega otro orden al config, que sea
    a sabiendas y no porque el código lo arma solo."""
    zp = zonaprop.build({"sitemap": None, "extra_orders": ["-orden-precio-ascendente"]})
    reordenada = zp._ruta_base("departamentos", "Palermo", "-orden-precio-ascendente")
    assert zp._url(reordenada, 1) == (
        "https://www.zonaprop.com.ar/departamentos-venta-palermo-orden-precio-ascendente.html"
    )
    # El Allow cubre esa URL exacta, no su paginación: una sola página.
    assert zp._paginas_de(reordenada) == 1
    assert zp._paginas_de(zp._ruta_base("departamentos", "Palermo")) == 5

    # Argenprop no permite combinar orden con paginación (`Disallow: /*?*&*`),
    # así que el orden nunca lleva `&pagina-N` pegado.
    ap = argenprop.build({"extra_orders": ["orden-menorprecio"]})
    con_orden = ap._url("/departamentos/venta/palermo", 2, "orden-menorprecio")
    assert con_orden == "https://www.argenprop.com/departamentos/venta/palermo?orden-menorprecio"
    assert "&" not in con_orden

    # Sin configurar, las URLs son las de siempre.
    assert zonaprop.build({})._url("/departamentos-venta-palermo.html", 2).endswith(
        "-pagina-2.html"
    )
    assert argenprop.build({})._url("/departamentos/venta/palermo", 2).endswith("?pagina-2")




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

def remax_avisos():
    state = (FIXTURES / "remax_state.json").read_text(encoding="utf-8")
    return remax.listings_from_state(state), state


def test_remax_map():
    """Remax se lee del transfer state, no del DOM: el JSON que alimenta las
    tarjetas llega con el HTML inicial (2,0 s contra 7,8 s esperando a que
    Angular dibuje) y el `pageSize` de la URL se reenvía a su API, así que
    entran 100 avisos por página en vez de 24."""
    avisos, _ = remax_avisos()
    item = remax.build({})._map(avisos[0], "Almagro")

    assert_esquema_comun(item)
    assert item["id"] == "remax:venta-departamento-dos-ambientes-con-balcon"
    assert (item["price"], item["currency"]) == (75_000, "USD")
    # Las dos superficies vienen como números: se terminó el "33.420" que
    # obligaba a adivinar si el punto era decimal o de miles.
    assert (item["covered_area"], item["total_area"]) == (25.5, 39.04)
    assert (item["rooms"], item["bedrooms"], item["bathrooms"]) == (2, 1, 1)
    assert item["maintenance_fee"] == 75_000
    assert item["photo_count"] == 3
    # El barrio solo está en el state: la tarjeta del HTML no lo expone.
    assert (item["neighborhood"], item["city"]) == ("Almagro", "Capital Federal")
    # GeoJSON viene [lon, lat]: si se dieran vuelta, los puntos caerían en
    # el océano Antártico y nadie lo notaría hasta abrir el mapa.
    assert (item["latitude"], item["longitude"]) == (-34.6102, -58.4201)


def test_remax_no_inventa_datos_que_el_aviso_no_trae():
    """Remax rellena con 0 lo que no publica, y un 0 que entra como dato es
    peor que un nulo: un monoambiente con 0 m² cubiertos dividiría el precio
    por cero al calcular el precio/m²."""
    avisos, _ = remax_avisos()
    item = remax.build({})._map(avisos[1], "Almagro")

    assert item["covered_area"] is None      # venía 0
    assert item["bedrooms"] is None          # venía 0
    assert item["total_area"] == 32
    assert item["photo_count"] == 0
    assert "maintenance_fee" not in item     # sin moneda de expensas
    assert item.get("latitude") is None      # sin location, no se le presta
                                             # la del aviso anterior


def test_remax_descarta_los_emprendimientos():
    """Igual que en Zonaprop y MercadoLibre: el edificio publica el precio
    de la unidad más chica contra el rango de superficies, y cruzar las dos
    puntas fabrica un descuento que no existe."""
    avisos, _ = remax_avisos()
    assert remax.build({})._map(avisos[2], "Almagro") is None


def test_remax_le_pega_la_provincia_al_barrio():
    """Remax no responde 404 ante un slug que no reconoce: devuelve otra
    búsqueda. `-en-palermo` es un landing residual de 1 aviso y
    `-en-villa-urquiza` da 0, mientras los de verdad tienen 1466 y 452."""
    source = remax.build({"zone_suffix": "-capital-federal", "page_size": 100})

    # `page` es 0-indexado del lado de Remax: nuestra página 1 es su page=0.
    assert source._url("Palermo", 1) == (
        "https://www.remax.com.ar/departamentos-en-venta-en-palermo-capital-federal"
        "?page=0&pageSize=100"
    )
    assert "-en-villa-urquiza-capital-federal?page=2" in source._url("Villa Urquiza", 3)
    # Sin configurar, sin sufijo: buscar fuera de CABA no lo necesita.
    assert "-en-palermo?" in remax.build({})._url("Palermo", 1)


def test_remax_se_da_cuenta_cuando_le_devuelven_otra_busqueda():
    """El fallo que metió 148 avisos de Allen, San Jerónimo y Mar del Plata
    en la base, y que en la corrida del 21/09 cortó "Belgrano R" cuando
    Remax contestó con Beccar, San Isidro y Belén de Escobar."""
    palermo = ["Palermo, Capital Federal", "Palermo Chico, Capital Federal"]
    assert not remax.busqueda_degradada(palermo, "Palermo")
    del_pais = ["Beccar, San Isidro", "Belén de Escobar, Escobar", "Belgrano, Capital Federal"]
    assert remax.busqueda_degradada(del_pais, "Belgrano R")
    # Esos mismos resultados son legítimos si lo que pedimos era Belgrano.
    assert not remax.busqueda_degradada(del_pais, "Belgrano")
    # Sin etiquetas no se acusa: si Remax cambia el formato del state, el
    # scraper sigue trayendo avisos en vez de cortar todas las zonas.
    assert not remax.busqueda_degradada([], "Palermo")


def test_remax_reconoce_el_final_de_la_lista():
    """Pasarse de la última página devuelve la lista vacía, no un 404. Si
    eso contara como falla, la zona quedaría marcada incompleta y sus avisos
    vendidos no se darían de baja nunca."""
    assert remax.listings_from_state('{"1": {"b": {"data": {"data": []}}, '
                                     '"u": "api/findAllWithEntrepreneurships?page=99"}}') == []
    assert remax.listings_from_state("") == []
    assert remax.listings_from_state("no soy json") == []
    avisos, _ = remax_avisos()
    assert len(avisos) == 3


def test_remax_lee_los_barrios_del_estado_de_la_pagina():
    _, state = remax_avisos()
    assert sorted(set(remax.geo_labels(state))) == [
        "Almagro, Capital Federal", "Boedo, Capital Federal",
    ]
    assert not remax.busqueda_degradada(remax.geo_labels(state), "Almagro")
    assert remax.busqueda_degradada(remax.geo_labels(state), "Villa Urquiza")
    assert remax.geo_labels("") == []


class _PaginaLlena:
    """Una página que siempre devuelve tarjetas: simula un inventario que no
    se termina nunca dentro del tope que permite el robots.txt."""

    def __init__(self, card):
        self._card = card

    def goto(self, *a, **k):
        return None

    def wait_for_selector(self, *a, **k):
        return None

    def query_selector_all(self, _sel):
        return [self._card]

    def query_selector(self, _sel):
        return None  # sin encabezado: el total queda sin dato, no rompe


def test_una_fuente_que_no_puede_agotar_la_zona_no_da_de_baja_nada():
    """El 21/09 se dieron de baja 815 avisos de Zonaprop en una sola corrida
    y los cuatro que se revisaron a mano seguían publicados.

    El robots.txt de Zonaprop permite 5 páginas y Palermo tiene 575: nunca
    vemos el inventario completo. Mientras Cloudflare cortaba, las zonas
    quedaban marcadas incompletas y el bug estaba tapado; al arreglar el
    corte, "no apareció en la corrida" pasó a leerse como "se vendió".

    Quien no puede llegar al final de la lista no tiene derecho a dar de
    baja por ausencia, y `incomplete_zones` es lo que se lo impide.
    """
    card = card_from("zonaprop_card.html", "[data-posting-type]")
    source = zonaprop.build(
        # sin rate limit ni sitemap: el test no pide nada por red
        {"new_session_per_page": False, "max_pages": 5, "rate_limit_seconds": 0,
         "sitemap": None}
    )

    avisos = list(
        source._fetch_pages(_PaginaLlena(card), "/departamentos-venta-palermo.html", "Palermo", 5)
    )

    assert len(avisos) == 5                    # una tarjeta por página
    # La zona no está "rota": se leyó bien, hasta donde el robots.txt deja.
    # Esa diferencia es la que decide si un aviso ausente se da de baja ya
    # (nunca acá) o recién tras storage.max_missed_runs corridas.
    assert source.capped_zones == {"Palermo"}
    assert source.incomplete_zones == set()


def test_mudafy_sin_red_no_cuenta_ausencias(monkeypatch, tmp_path):
    """Si no responde ni un listado ni el sitemap, las zonas quedan como
    incompletas: si quedaran "leídas con tope", cada aviso sumaría una
    corrida sin verse y a la séptima caída de red se darían de baja."""
    fuente = mudafy.build({"fichas_cache": str(tmp_path / "cache.json"), "rate_limit_seconds": 0})
    monkeypatch.setattr(fuente, "_candidatos_de_tarjetas", lambda zonas: {})
    monkeypatch.setattr(fuente, "_candidatos_del_sitemap", lambda: {})

    assert list(fuente.fetch("Palermo", {"zones": ["Palermo"]})) == []
    assert fuente.incomplete_zones == {"Palermo"}


def test_remax_archiva_cada_aviso_en_el_barrio_que_declara():
    """Remax no tiene búsqueda propia para Cañitas ni para Belgrano R: sus
    avisos vienen adentro de Palermo y Belgrano, con su geoLabel."""
    avisos, _ = remax_avisos()
    zonas = ["Palermo", "Cañitas", "Belgrano", "Belgrano R", "Almagro"]
    fuente = remax.build({})

    canitas = dict(avisos[0], geoLabel="Las Cañitas, Capital Federal")
    assert fuente._map(canitas, "Palermo", zonas)["zone"] == "Cañitas"
    belgrano_r = dict(avisos[0], geoLabel="Belgrano R, Capital Federal")
    assert fuente._map(belgrano_r, "Belgrano", zonas)["zone"] == "Belgrano R"
    # Un vecino que no es zona del config se queda en la que se buscó.
    boedo = dict(avisos[0], geoLabel="Boedo, Capital Federal")
    assert fuente._map(boedo, "Almagro", zonas)["zone"] == "Almagro"


def _estado_remax(enteras=(), con_tope=(), rotas=(), degradadas=(), aporto=None):
    fuente = remax.build({})
    fuente._enteras, fuente._con_tope = set(enteras), set(con_tope)
    fuente._rotas, fuente._degradadas = set(rotas), set(degradadas)
    fuente._aporto = {k: set(v) for k, v in (aporto or {}).items()}
    fuente._recalcular_estado()
    return fuente.incomplete_zones, fuente.capped_zones


def test_remax_un_sub_barrio_hereda_como_se_leyo_su_barrio():
    aporto = {"Palermo": {"Palermo", "Cañitas"}}
    # Palermo se leyó hasta el final: Cañitas también, se puede dar de baja.
    assert _estado_remax(enteras=["Palermo"], degradadas=["Cañitas"], aporto=aporto) == (set(), set())
    # Palermo llegó al tope de páginas: Cañitas también, baja paciente.
    assert _estado_remax(con_tope=["Palermo"], degradadas=["Cañitas"], aporto=aporto) == (
        set(), {"Palermo", "Cañitas"},
    )
    # Palermo se cortó: de Cañitas no se sabe nada.
    assert _estado_remax(rotas=["Palermo"], degradadas=["Cañitas"], aporto=aporto) == (
        {"Palermo", "Cañitas"}, set(),
    )
    # Una zona degradada que ninguna búsqueda cubrió sigue incompleta.
    assert _estado_remax(enteras=["Palermo"], degradadas=["Quilmes"], aporto=aporto) == (
        {"Quilmes"}, set(),
    )


def test_el_total_de_cada_busqueda_sale_del_encabezado():
    from inmobot.sources._text import parse_total

    # El punto de un conteo siempre separa miles: nunca es 11,972 avisos.
    assert parse_total("11.972 Departamentos en venta en Palermo, CABA") == 11_972
    assert parse_total("1 Departamento en venta en Cañitas, Córdoba") == 1
    assert parse_total("Departamentos en venta") is None

    assert mercadolibre.total_declarado(
        '<span class="ui-search-search-result__quantity-results">5.815 resultados</span>'
    ) == 5_815
    assert mudafy.total_declarado(
        "<title>1.195 departamentos en venta en CABA Palermo | Mudafy</title>"
    ) == 1_195
    # Un título sin conteo no es un total, aunque tenga números en otro lado.
    assert mudafy.total_declarado("<title>Departamentos en venta en Guayaquil 720</title>") is None

    _, state = remax_avisos()
    assert remax.total_from_state(state) == 3
    assert remax.total_from_state("no es json") is None


class _RespuestaML:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_mercadolibre_busca_cada_sub_barrio_y_archiva_todo_en_la_zona(monkeypatch):
    """Una página por búsqueda: 48 de los 5.813 avisos de Palermo. Cada
    sub-barrio trae su propia página, y todo cuenta como Palermo."""
    tarjetas = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")

    def pagina(barrio, total):
        return (
            f"<h1>Departamentos en Venta Propiedades individuales en {barrio}, Capital Federal</h1>"
            f'<span class="ui-search-search-result__quantity-results">{total} resultados</span>'
            + tarjetas
        )

    respuestas = {
        "palermo": pagina("Palermo", "5.813"),
        "palermo-soho": pagina("Palermo Soho", "759"),
        # Lo que ML devuelve ante un barrio que no conoce: una búsqueda por texto.
        "botanico": "<h1>Capital federal botanico</h1>" + tarjetas,
    }
    pedidas = []
    fuente = mercadolibre.build({"rate_limit_seconds": 0})

    def get(url):
        barrio = url.rstrip("/").rsplit("/", 1)[-1]
        pedidas.append(barrio)
        return _RespuestaML(respuestas[barrio])

    monkeypatch.setattr(fuente.client, "get", get)
    subzonas = {"Palermo": ["Palermo Soho", "Botánico"]}
    items = list(fuente.fetch("Palermo", {"subzones": subzonas}))

    # Primero el barrio, después cada sub-barrio en el orden del config.
    assert pedidas == ["palermo", "palermo-soho", "botanico"]
    # Los mismos avisos en dos búsquedas no se cuentan dos veces.
    assert len(items) == len({i["id"] for i in items}) > 0
    assert {i["zone"] for i in items} == {"Palermo"}
    # El total es el del barrio entero: contra eso se mide la cobertura.
    assert fuente.totals == {"Palermo": 5813}
    assert mercadolibre.es_la_busqueda(respuestas["palermo-soho"], "Palermo Soho")
    assert not mercadolibre.es_la_busqueda(respuestas["botanico"], "Botánico")


def test_mercadolibre_usa_el_nombre_del_barrio_en_ml_y_valida_la_zona(monkeypatch):
    """La zona "Cañitas" para ML es "Las Cañitas". Buscando "canitas" no da
    error: hace una búsqueda por texto, con otro total (1.115 contra 995) y
    avisos de Palermo y Belgrano mezclados."""
    tarjetas = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")
    respuestas = {
        "las-canitas": (
            "<h1>Departamentos en Venta Propiedades individuales en Las Cañitas, Capital Federal</h1>"
            '<span class="ui-search-search-result__quantity-results">995 resultados</span>' + tarjetas
        ),
        "canitas": "<h1>Capital federal canitas</h1>" + tarjetas,
    }
    pedidas = []

    def fuente_con(conf):
        fuente = mercadolibre.build(dict(conf, rate_limit_seconds=0))

        def get(url):
            barrio = url.rstrip("/").rsplit("/", 1)[-1]
            pedidas.append(barrio)
            return _RespuestaML(respuestas[barrio])

        monkeypatch.setattr(fuente.client, "get", get)
        return fuente

    buena = fuente_con({"zone_aliases": {"Cañitas": "Las Cañitas"}})
    items = list(buena.fetch("Cañitas", {"zones": ["Cañitas"]}))
    assert pedidas == ["las-canitas"]
    assert items and buena.totals == {"Cañitas": 995}
    assert "Cañitas" not in buena.incomplete_zones

    # Sin el alias: la búsqueda por texto no se guarda y la zona no cuenta
    # ausencias, así no se dan de baja los avisos que ya teníamos.
    sin_alias = fuente_con({})
    assert list(sin_alias.fetch("Cañitas", {"zones": ["Cañitas"]})) == []
    assert sin_alias.incomplete_zones == {"Cañitas"}
    assert sin_alias.totals == {}


def _ml_falso(monkeypatch, fuente, paginas):
    """Un ML de mentira: `paginas` va de la ruta de búsqueda a (título, total)
    o a un código de error. Cada búsqueda devuelve dos avisos propios."""
    import httpx

    tarjetas = (FIXTURES / "mercadolibre_page.html").read_text(encoding="utf-8")
    pedidas = []

    def get(url):
        ruta = url.split("propiedades-individuales/", 1)[1].replace("capital-federal/", "")
        pedidas.append(ruta)
        pedido = httpx.Request("GET", url)
        valor = paginas[ruta]
        if isinstance(valor, int):
            return httpx.Response(valor, request=pedido)
        titulo, total = valor
        n = len(pedidas)
        html = (
            f"<h1>{titulo}</h1>"
            f'<span class="ui-search-search-result__quantity-results">{total} resultados</span>'
            + tarjetas.replace("2000000002", f"2{n:09d}").replace("3000000003", f"3{n:09d}")
        )
        return httpx.Response(200, text=html, request=pedido)

    monkeypatch.setattr(fuente.client, "get", get)
    return pedidas


_PALERMO = "Departamentos en Venta Propiedades individuales en Palermo, Capital Federal"


def test_mercadolibre_parte_solo_lo_que_no_entra_en_una_pagina(monkeypatch):
    fuente = mercadolibre.build({
        "rate_limit_seconds": 0,
        "split_rooms": ["1-ambiente", "2-ambientes"],
        "split_age": ["0años-0años", "1años-15años"],
    })
    pedidas = _ml_falso(monkeypatch, fuente, {
        "palermo/": (_PALERMO, "500"),
        "1-ambiente/palermo/": (_PALERMO + ", 1 ambiente", "30"),      # entra: no se parte
        "2-ambientes/palermo/": (_PALERMO + ", 2 ambientes", "200"),   # no entra: por antigüedad
        "2-ambientes/palermo/_PROPERTY*AGE_0años-0años": (_PALERMO + ", 2 ambientes", "150"),
        # Más que su búsqueda madre: ML no aplicó el filtro, se descarta.
        "2-ambientes/palermo/_PROPERTY*AGE_1años-15años": (_PALERMO + ", 2 ambientes", "900"),
    })
    items = list(fuente.fetch("Palermo", {"zones": ["Palermo"]}))

    assert pedidas == [
        "palermo/", "1-ambiente/palermo/", "2-ambientes/palermo/",
        "2-ambientes/palermo/_PROPERTY*AGE_0años-0años",
        "2-ambientes/palermo/_PROPERTY*AGE_1años-15años",
    ]
    # Dos avisos por búsqueda válida (4 de 5), todos distintos, en Palermo.
    assert len(items) == len({i["id"] for i in items}) == 8
    assert fuente.totals == {"Palermo": 500}


def test_mercadolibre_reparte_el_tope_por_tamano_y_frena_ante_un_429(monkeypatch):
    # Tope 6: se van 2 en las búsquedas de las zonas y quedan 4, que se
    # reparten en proporción. Palermo (500 de 520) se lleva 3; Colegiales
    # (20) entra entera en una página y no necesita ninguna.
    fuente = mercadolibre.build({
        "rate_limit_seconds": 0, "max_searches_per_run": 6,
        "split_rooms": ["1-ambiente", "2-ambientes", "3-ambientes"],
    })
    colegiales = "Departamentos en Venta Propiedades individuales en Colegiales, Capital Federal"
    pedidas = _ml_falso(monkeypatch, fuente, {
        "palermo/": (_PALERMO, "500"),
        "colegiales/": (colegiales, "20"),
        "1-ambiente/palermo/": (_PALERMO + ", 1 ambiente", "100"),
        "2-ambientes/palermo/": 429,
    })
    buscar = {"zones": ["Palermo", "Colegiales"]}
    list(fuente.fetch("Palermo", buscar))
    list(fuente.fetch("Colegiales", buscar))

    # Después del 429 no se pide nada más: 3 ambientes queda sin pedir.
    assert pedidas == ["palermo/", "colegiales/", "1-ambiente/palermo/", "2-ambientes/palermo/"]
    # El 429 corta la fuente: Palermo no cuenta ausencias esta corrida.
    assert "Palermo" in fuente.incomplete_zones
    assert "Colegiales" not in fuente.incomplete_zones


def _argenprop_con_paginas(monkeypatch, secuencia):
    """Argenprop contra páginas de mentira: cada navegación consume el
    siguiente valor de `secuencia` ("reto" = verificación de Cloudflare,
    "ok" = una página que carga). Devuelve la fuente y las URLs pedidas."""
    from contextlib import contextmanager

    pedidas = []
    restantes = list(secuencia)

    class Pagina:
        def goto(self, url, **k):
            pedidas.append(url)
            self._estado = restantes.pop(0) if restantes else "ok"

        def wait_for_selector(self, *a, **k):
            if self._estado == "reto":
                raise TimeoutError("sin tarjetas")

        def title(self):
            return "Just a moment..." if self._estado == "reto" else "Departamentos en venta"

        def inner_text(self, _sel):
            return "Let's confirm you are human" if self._estado == "reto" else "20 resultados"

        def query_selector_all(self, _sel):
            return []

    @contextmanager
    def pagina():
        yield Pagina()

    monkeypatch.setattr(argenprop, "browser_page", pagina)
    monkeypatch.setattr(argenprop.time, "sleep", lambda s: None)
    fuente = argenprop.build({"sitemap": None, "max_challenges_in_a_row": 3})
    monkeypatch.setattr(fuente, "_rutas_de", lambda zone, ps, zonas: [f"/{zone}/a", f"/{zone}/b", f"/{zone}/c"])
    return fuente, pedidas


def test_argenprop_deja_de_insistir_despues_de_tres_verificaciones_seguidas(monkeypatch):
    """El 24/09 fueron 50+ verificaciones en 20 minutos: cortaba cada
    búsqueda y pasaba a la siguiente. Tres seguidas ya dicen que no."""
    fuente, pedidas = _argenprop_con_paginas(monkeypatch, ["reto"] * 10)
    zonas = {"zones": ["palermo", "belgrano"]}
    for zona in zonas["zones"]:
        assert list(fuente.fetch(zona, zonas)) == []

    assert len(pedidas) == 3
    # Ninguna de las dos zonas se leyó: ninguna puede dar de baja avisos.
    assert fuente.incomplete_zones == {"palermo", "belgrano"}


def test_argenprop_una_pagina_que_carga_reinicia_la_cuenta(monkeypatch):
    fuente, pedidas = _argenprop_con_paginas(monkeypatch, ["reto", "reto", "ok", "reto", "reto", "ok"])
    monkeypatch.setattr(fuente, "_rutas_de", lambda zone, ps, zonas: [f"/{zone}/{n}" for n in range(6)])
    list(fuente.fetch("palermo", {"zones": ["palermo"]}))
    assert not fuente._frenado
    assert len(pedidas) == 6
