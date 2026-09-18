"""Funciones puras de normalize.py: moneda, filtros y fingerprint.

Son las que toca todo aviso de todas las fuentes, así que un cambio acá se
propaga a la base entera sin hacer ruido.
"""

from inmobot.normalize import fingerprint, passes_filters, to_currency

FX = {"ARS_per_USD": 1500}

SEARCH = {
    "price_min": 50_000,
    "price_max": 140_000,
    "filters": {"covered_area_min": 40, "rooms_min": 2, "require_photos": True},
}


def listing(**overrides) -> dict:
    item = {"price_norm": 120_000, "covered_area": 55, "rooms": 3, "photo_count": 4}
    item.update(overrides)
    return item


# --- to_currency ------------------------------------------------------- #

def test_to_currency_misma_moneda_no_toca_el_numero():
    assert to_currency(120_000, "USD", "USD", FX) == 120_000


def test_to_currency_convierte_pesos_a_dolares():
    assert to_currency(150_000_000, "ARS", "USD", FX) == 100_000


def test_to_currency_sin_cotizacion_devuelve_none():
    # None y no cero: un aviso que no se puede comparar se descarta, no entra
    # a la mediana de su zona valiendo nada.
    assert to_currency(150_000_000, "ARS", "USD", {}) is None


def test_to_currency_sin_monto_o_sin_moneda_devuelve_none():
    assert to_currency(None, "USD", "USD", FX) is None
    assert to_currency(120_000, None, "USD", FX) is None


# --- passes_filters ---------------------------------------------------- #

def test_passes_filters_acepta_un_aviso_en_rango():
    assert passes_filters(listing(), SEARCH) == (True, "")


def test_passes_filters_rechaza_por_precio_con_el_motivo():
    ok, reason = passes_filters(listing(price_norm=200_000), SEARCH)
    assert ok is False
    assert reason == "precio por encima del rango"


def test_passes_filters_acepta_un_aviso_sin_el_dato_filtrado():
    # Decisión deliberada: un dato ausente no descalifica. Prefiero revisar a
    # mano un aviso sin m² publicados que perderlo. Este test está para que no
    # se "arregle" sin querer.
    ok, _ = passes_filters(listing(covered_area=None), SEARCH)
    assert ok is True


def test_passes_filters_rechaza_un_aviso_sin_precio_normalizable():
    assert passes_filters(listing(price_norm=None), SEARCH) == (
        False, "sin precio normalizable",
    )


def test_passes_filters_rechaza_un_aviso_sin_fotos():
    ok, reason = passes_filters(listing(photo_count=0), SEARCH)
    assert ok is False
    assert reason == "sin fotos"


# --- fingerprint ------------------------------------------------------- #

def test_fingerprint_agrupa_dos_publicaciones_del_mismo_inmueble():
    # Mismo depto publicado por dos inmobiliarias, con los m² y el precio
    # apenas distintos: tiene que caer en la misma huella.
    a = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 115_000}
    b = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50.8, "price_norm": 117_000}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_separa_inmuebles_distintos():
    a = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 115_000}
    b = {"neighborhood": "Almagro", "rooms": 4, "covered_area": 120, "price_norm": 260_000}
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_separa_por_precio_aunque_el_resto_sea_igual():
    # Regresión: la cubeta de precio se calculaba con un paso proporcional al
    # propio precio, así que `precio / paso` daba siempre 20 y el precio no
    # participaba de la huella. Dos deptos del mismo barrio, ambientes y m²
    # quedaban como "el mismo inmueble" costara lo que costara.
    barato = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 80_000}
    caro = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 160_000}
    assert fingerprint(barato) != fingerprint(caro)


def test_fingerprint_es_estable_entre_corridas():
    # Hardcodeado a propósito: si cambia la forma de armar la clave, las
    # huellas viejas de la base dejan de matchear con las nuevas y los
    # duplicados detectados hasta hoy se pierden en silencio.
    item = {"neighborhood": "Almagro", "rooms": 2, "covered_area": 50, "price_norm": 115_000}
    assert fingerprint(item) == "2ed318b6f4ba8ac1"
