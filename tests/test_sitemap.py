"""Descubrimiento de búsquedas desde el sitemap del portal.

Es lo que separa 58 avisos de Palermo de 145: armando la URL a mano solo se
llega a `/departamentos/venta/palermo`, y el robots.txt corta en la página 3.
El sitemap dice que el portal también indexa palermo-soho, palermo-chico,
palermo-hollywood... y cada una es otra búsqueda con sus propias 3 páginas.
"""

import gzip

import httpx

from inmobot.sources import _sitemap

BASE = "https://www.argenprop.com"

XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{b}/departamentos/venta/palermo</loc></url>
  <url><loc>{b}/departamentos/venta/palermo-soho</loc></url>
  <url><loc>{b}/departamentos/venta/palermo-soho/monoambiente</loc></url>
  <url><loc>{b}/departamentos/venta/belgrano</loc></url>
  <url><loc>{b}/departamentos/venta/belgrano-r</loc></url>
  <url><loc>{b}/casas/venta/palermo</loc></url>
  <url><loc>{b}/departamentos/alquiler/palermo</loc></url>
  <url><loc>{b}/departamentos/venta/mataderos</loc></url>
</urlset>""".format(b=BASE)

INDICE = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{b}/sitemaps/parte-01.xml.gz</loc></sitemap>
</sitemapindex>""".format(b=BASE)


def cliente_falso(respuestas: dict[str, bytes]) -> httpx.Client:
    def responder(request: httpx.Request) -> httpx.Response:
        cuerpo = respuestas.get(str(request.url))
        if cuerpo is None:
            return httpx.Response(404)
        return httpx.Response(200, content=cuerpo)

    return httpx.Client(transport=httpx.MockTransport(responder))


def test_sigue_el_indice_y_descomprime():
    """El sitemap grande es un índice que apunta a los pedazos, y vienen
    gzipeados: dos capas antes de ver una sola URL."""
    respuestas = {
        f"{BASE}/sitemaps/todo.xml.gz": gzip.compress(INDICE.encode()),
        f"{BASE}/sitemaps/parte-01.xml.gz": gzip.compress(XML.encode()),
    }
    with cliente_falso(respuestas) as client:
        urls = _sitemap.descargar(f"{BASE}/sitemaps/todo.xml.gz", client)
    assert f"{BASE}/departamentos/venta/palermo-soho" in urls
    assert len(urls) == 8


def test_un_sitemap_caido_no_rompe_el_scrape():
    """Sin sitemap se scrapea peor, no se deja de scrapear."""
    with cliente_falso({}) as client:
        assert _sitemap.descargar(f"{BASE}/sitemaps/no-existe.xml.gz", client) == []


def test_agrupa_los_sub_barrios_bajo_su_zona():
    rutas = _sitemap.rutas_por_zona(
        _locs(), ["Palermo", "Belgrano"], "departamentos", "venta", BASE
    )
    assert rutas["Palermo"] == [
        "/departamentos/venta/palermo",
        "/departamentos/venta/palermo-soho",
        "/departamentos/venta/palermo-soho/monoambiente",
    ]
    # Otro tipo de propiedad y otra operación no entran, y un barrio que no
    # está en el config tampoco.
    assert "/casas/venta/palermo" not in rutas["Palermo"]
    assert "/departamentos/alquiler/palermo" not in rutas["Palermo"]
    assert all("mataderos" not in r for v in rutas.values() for r in v)


def test_belgrano_r_no_se_lo_queda_belgrano():
    """`belgrano-r` empieza con `belgrano`, así que la zona más amplia se lo
    llevaría puesto. Si eso pasa, sus avisos quedan archivados bajo el
    barrio equivocado y contaminan las medianas de los dos."""
    zonas = ["Belgrano", "Belgrano R"]
    rutas = _sitemap.rutas_por_zona(_locs(), zonas, "departamentos", "venta", BASE)

    assert rutas["Belgrano"] == ["/departamentos/venta/belgrano"]
    assert rutas["Belgrano R"] == ["/departamentos/venta/belgrano-r"]

    # Y si Belgrano R no está configurada, sus avisos sí son de Belgrano.
    solo = _sitemap.rutas_por_zona(_locs(), ["Belgrano"], "departamentos", "venta", BASE)
    assert solo["Belgrano"] == [
        "/departamentos/venta/belgrano",
        "/departamentos/venta/belgrano-r",
    ]


def _locs() -> list[str]:
    return [l for l in _sitemap._locs(XML)]


def test_la_rotacion_recorre_todas_las_busquedas_en_pocos_dias(monkeypatch):
    """Las 18 búsquedas de Palermo son 54 páginas seguidas y Cloudflare
    corta mucho antes: en la prueba del 21/09 pasaron 8. Pedirlas todas de
    una no trae más datos, trae más bloqueos.

    La rotación toma un tramo por corrida. La del barrio entero va siempre
    porque es la más amplia; el resto se reparte y se cubre en unos días.
    """
    import datetime as dt
    from inmobot.sources import argenprop

    source = argenprop.build({"sitemap": None, "max_searches_per_zone": 4})
    source._rutas = {
        "Palermo": [f"/departamentos/venta/palermo-{i}" for i in range(9)]
    }
    source.sitemap = "x"   # ya está cacheado, no se baja nada

    vistas, propia = set(), "/departamentos/venta/palermo"
    for dia in range(1, 6):
        class Fecha(dt.date):
            @classmethod
            def today(cls):
                return dt.date(2026, 1, 1) + dt.timedelta(days=dia - 1)

        monkeypatch.setattr(argenprop, "date", Fecha)
        rutas = source._rutas_de("Palermo", "departamentos", ["Palermo"])

        assert len(rutas) == 4
        assert rutas[0] == propia, "la búsqueda del barrio entero va siempre"
        assert len(set(rutas)) == 4, "sin repetir dentro de la misma corrida"
        vistas |= set(rutas)

    # 9 búsquedas repartidas de a 3 por corrida: en 5 días ya se vieron todas.
    assert vistas == {propia} | set(source._rutas["Palermo"])


def test_sin_cupo_se_piden_todas():
    from inmobot.sources import argenprop

    source = argenprop.build({"sitemap": None, "max_searches_per_zone": 0})
    source._rutas = {"Palermo": ["/departamentos/venta/palermo-soho"]}
    source.sitemap = "x"
    assert source._rutas_de("Palermo", "departamentos", ["Palermo"]) == [
        "/departamentos/venta/palermo",
        "/departamentos/venta/palermo-soho",
    ]


def test_zona_de_barrio_traduce_lo_que_declara_la_ficha():
    zonas = ["Palermo", "Belgrano", "Belgrano R", "Cañitas", "Villa Urquiza"]
    assert _sitemap.zona_de_barrio("Palermo Soho", zonas) == "Palermo"
    assert _sitemap.zona_de_barrio("Las Cañitas", zonas) == "Cañitas"
    # Contiene "belgrano", pero es la más específica la que gana.
    assert _sitemap.zona_de_barrio("Belgrano R", zonas) == "Belgrano R"
    assert _sitemap.zona_de_barrio("Belgrano", zonas) == "Belgrano"
    # "Villa" sola no alcanza: la zona entera tiene que estar.
    assert _sitemap.zona_de_barrio("Villa Crespo", zonas) is None
    assert _sitemap.zona_de_barrio(None, zonas) is None
