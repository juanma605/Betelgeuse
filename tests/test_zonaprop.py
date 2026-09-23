from bs4 import BeautifulSoup
from fake_dom import FIXTURES, FakeElement

from inmobot.sources import zonaprop

PAGINAS = {
    "/departamentos-venta-belgrano.html": [
        ("Belgrano", "Capital Federal"), ("Belgrano C", "Belgrano"),
    ],
    # Termina en "belgrano", así que el sitemap la asigna a Belgrano.
    "/departamentos-venta-villa-general-belgrano.html": [
        ("Villa General Belgrano", "Córdoba"), ("Centro", "Villa General Belgrano"),
    ],
    "/departamentos-venta-belgrano-chico-belgrano.html": [
        ("Belgrano Chico", "Belgrano"), ("Cañitas", "Córdoba"),
    ],
}


def _fuente(monkeypatch):
    monkeypatch.setattr(zonaprop.time, "sleep", lambda s: None)
    src = zonaprop.ZonapropSource({"sitemap": None, "max_pages": 2})
    pedidas = []

    def traer(page_obj, url, ruta, zone, n):
        pedidas.append(ruta)
        avisos = [
            {"id": f"{ruta}{n}{i}", "neighborhood": b, "city": c}
            for i, (b, c) in enumerate(PAGINAS[ruta])
        ]
        return avisos, True

    monkeypatch.setattr(src, "_traer_pagina", traer)
    monkeypatch.setattr(src, "_rutas_de", lambda zone, ps, zonas: list(PAGINAS))
    return src, pedidas


def test_rutas_del_sitemap_de_otro_lugar_se_descartan(monkeypatch):
    src, pedidas = _fuente(monkeypatch)
    lugares = {(a["neighborhood"], a["city"]) for a in src.fetch("Belgrano", {})}

    assert lugares == {
        ("Belgrano", "Capital Federal"),
        ("Belgrano C", "Belgrano"),
        ("Belgrano Chico", "Belgrano"),
    }
    # La ruta de Córdoba se deja de paginar apenas se ve que es de otro lado.
    assert pedidas.count("/departamentos-venta-villa-general-belgrano.html") == 1


def test_la_busqueda_base_sale_del_sitemap_si_la_armada_no_existe():
    """`departamentos-venta-belgrano-r.html` no es un slug de Zonaprop y no
    da 404: devuelve Belgrano entero, y 5 páginas de Belgrano se guardaban
    como Belgrano R (772 avisos contra 567 que tiene el barrio). El sitemap
    la nombra `belgrano-r-belgrano`."""
    src = zonaprop.ZonapropSource({"sitemap": "x", "max_searches_per_zone": 0})
    src._rutas = {
        "Belgrano R": [
            "/departamentos-venta-belgrano-r-belgrano-2-habitaciones.html",
            "/departamentos-venta-belgrano-r-belgrano.html",
        ],
        "Palermo": [
            "/departamentos-venta-palermo-soho-palermo.html",
            "/departamentos-venta-palermo.html",
        ],
    }
    rutas = src._rutas_de("Belgrano R", "departamentos", ["Belgrano R"])
    assert rutas[0] == "/departamentos-venta-belgrano-r-belgrano.html"
    assert "/departamentos-venta-belgrano-r.html" not in rutas
    assert len(rutas) == 2

    # Si el sitemap sí la lista, la base sigue siendo la armada a mano.
    assert src._rutas_de("Palermo", "departamentos", ["Palermo"])[0] == (
        "/departamentos-venta-palermo.html"
    )


def test_cada_aviso_se_archiva_en_el_barrio_que_declara(monkeypatch):
    src, _ = _fuente(monkeypatch)
    monkeypatch.setattr(
        src, "_traer_pagina",
        lambda page_obj, url, ruta, zone, n: (
            [src._map(_tarjeta_en("Belgrano R, Belgrano"), zone)], False
        ),
    )
    monkeypatch.setattr(src, "_rutas_de", lambda zone, ps, zonas: ["/departamentos-venta-belgrano.html"])
    zonas = ["Belgrano", "Belgrano R"]
    (item,) = list(src.fetch("Belgrano", {"zones": zonas}))
    assert item["zone"] == "Belgrano R"


def _tarjeta_en(ubicacion: str):
    """La tarjeta real del fixture, con otra ubicación."""
    html = (FIXTURES / "zonaprop_card.html").read_text(encoding="utf-8")
    html = html.replace("Almagro, Capital Federal", ubicacion)
    return FakeElement(BeautifulSoup(html, "html.parser").select_one("[data-posting-type]"))


def test_el_total_de_la_zona_es_el_de_la_busqueda_base(monkeypatch):
    """Las rutas del sitemap son recortes de la zona (`-con-balcon`,
    `-2-habitaciones`): sumarlas contaría dos veces el mismo aviso. Y de la
    base, solo la página 1, que es donde está el encabezado."""
    src, _ = _fuente(monkeypatch)
    traer_original = src._traer_pagina

    def traer(page_obj, url, ruta, zone, n):
        src._ultimo_total = {"/departamentos-venta-belgrano.html": 6670}.get(ruta, 55) * n
        return traer_original(page_obj, url, ruta, zone, n)

    monkeypatch.setattr(src, "_traer_pagina", traer)
    list(src.fetch("Belgrano", {}))
    assert src.totals == {"Belgrano": 6670}
