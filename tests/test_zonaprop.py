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
