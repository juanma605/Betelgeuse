"""Buscador en lenguaje natural, sin red ni modelo: el cliente es un doble."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from inmobot import buscador, db
from inmobot.config import Config

ZONAS = ["Palermo", "Belgrano", "Belgrano R", "Cañitas", "Almagro"]


def ids(conn, filtros) -> set[str]:
    sql, params = buscador.construir_sql(filtros)
    return {fila[0] for fila in conn.execute(sql, params)}


@pytest.fixture
def conn(tmp_path):
    """Seis avisos donde cada filtro deja afuera a alguno distinto."""
    base = {"source": "x", "price_norm": 100_000, "rooms": 2, "bedrooms": 1,
            "bathrooms": 1, "covered_area": 50, "maintenance_fee": 80_000}
    avisos = [
        {"id": "palermo", "zone": "Palermo"},
        {"id": "belgrano3", "zone": "Belgrano", "rooms": 3, "bedrooms": 2, "price_norm": 140_000},
        {"id": "caro", "zone": "Palermo", "price_norm": 300_000, "bathrooms": 2},
        # Sin m² cubiertos: vale la superficie total, como en analyze.
        {"id": "solo_total", "zone": "Almagro", "covered_area": None, "total_area": 90},
        {"id": "expensas_altas", "zone": "Almagro", "maintenance_fee": 400_000},
        {"id": "alquiler", "zone": "Palermo", "operation": "alquiler", "price_norm": 900},
    ]
    with db.connect(tmp_path / "t.db") as c:
        db.upsert_listings(c, [
            {**base, "source_id": a["id"], **a} for a in avisos
        ])
    conexion = sqlite3.connect(tmp_path / "t.db")
    yield conexion
    conexion.close()


# --- construir_sql ------------------------------------------------------ #

def test_sin_filtros_trae_todos_los_activos(conn):
    sql, params = buscador.construir_sql({})
    assert sql == "SELECT id FROM listings WHERE active = 1"
    assert params == []
    assert len(ids(conn, {})) == 6


@pytest.mark.parametrize("filtros, esperados", [
    ({"zonas": ["Belgrano", "Almagro"]}, {"belgrano3", "solo_total", "expensas_altas"}),
    ({"operacion": "alquiler"}, {"alquiler"}),
    ({"ambientes_min": 3}, {"belgrano3"}),
    ({"ambientes_max": 2}, {"palermo", "caro", "solo_total", "expensas_altas", "alquiler"}),
    ({"dormitorios_min": 2}, {"belgrano3"}),
    ({"banos_min": 2}, {"caro"}),
    ({"precio_min": 200_000}, {"caro"}),
    ({"precio_max": 1_000}, {"alquiler"}),
    ({"m2_min": 60}, {"solo_total"}),
    ({"expensas_max": 100_000}, {"palermo", "belgrano3", "caro", "solo_total", "alquiler"}),
])
def test_cada_campo_solo(conn, filtros, esperados):
    assert ids(conn, filtros) == esperados


def test_campos_combinados(conn):
    filtros = {"zonas": ["Palermo", "Belgrano"], "operacion": "venta",
               "ambientes_min": 2, "precio_max": 150_000, "m2_min": 40}
    sql, params = buscador.construir_sql(filtros)
    assert params == ["Palermo", "Belgrano", "venta", 2, 150_000, 40]
    assert ids(conn, filtros) == {"palermo", "belgrano3"}


def test_la_inyeccion_en_un_valor_queda_como_parametro(conn):
    maligna = "Palermo'; DROP TABLE listings;--"
    sql, params = buscador.construir_sql({"zonas": [maligna]})
    assert "DROP" not in sql
    assert params == [maligna]
    assert ids(conn, {"zonas": [maligna]}) == set()
    # La tabla sigue ahí, con todo adentro.
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 6


def test_una_clave_desconocida_no_llega_al_sql():
    sql, params = buscador.construir_sql({"1=1 OR zone": "x", "precio_max": 5})
    assert sql == "SELECT id FROM listings WHERE active = 1 AND price_norm <= ?"
    assert params == [5]


def test_contra_la_base_de_demo():
    # El demo no tiene `operation`: se genera antes de esa migración, y el
    # dashboard la saca con quitar_sin_columna.
    conexion = sqlite3.connect("file:data/demo.db?mode=ro", uri=True)
    try:
        columnas = {f[1] for f in conexion.execute("PRAGMA table_info(listings)")}
        filtros, avisos = buscador.quitar_sin_columna(
            {"zonas": ["Palermo", "Belgrano"], "operacion": "venta",
             "ambientes_min": 3, "ambientes_max": 3, "precio_max": 300_000, "m2_min": 60},
            columnas,
        )
        assert "operacion" not in filtros and avisos
        sql, params = buscador.construir_sql(filtros)
        filas = conexion.execute(sql, params).fetchall()
    finally:
        conexion.close()
    assert filas


# --- validar_filtros ---------------------------------------------------- #

def test_descarta_claves_desconocidas_y_tipos_incorrectos():
    crudo = {
        "precio_max": 150_000,
        "sql": "DROP TABLE listings",       # clave desconocida
        "ambientes_min": "3",               # string: no se adivina
        "ambientes_max": 2.5,               # no es entero
        "dormitorios_min": True,            # bool no es número
        "banos_min": -1,                    # negativo
        "m2_min": float("nan"),
        "precio_min": "150.000",            # ¿150 o 150 mil? no se adivina
        "expensas_max": [100],
        "operacion": "permuta",
        "zonas": 42,
    }
    assert buscador.validar_filtros(crudo, ZONAS) == {
        "filtros": {"precio_max": 150_000.0}, "avisos": [],
    }


def test_acepta_lo_bien_formado():
    crudo = {"zonas": ["Palermo"], "operacion": "Venta", "ambientes_min": 3,
             "ambientes_max": 3.0, "m2_min": 60, "banos_min": 1}
    assert buscador.validar_filtros(crudo, ZONAS)["filtros"] == {
        "zonas": ["Palermo"], "operacion": "venta", "ambientes_min": 3,
        "ambientes_max": 3, "banos_min": 1, "m2_min": 60.0,
    }


def test_rango_al_reves_se_da_vuelta():
    filtros = buscador.validar_filtros({"precio_min": 200_000, "precio_max": 100_000}, ZONAS)["filtros"]
    assert (filtros["precio_min"], filtros["precio_max"]) == (100_000, 200_000)


@pytest.mark.parametrize("pedida, zona", [
    ("palermo", "Palermo"),
    ("Canitas", "Cañitas"),
    ("Las Cañitas", "Cañitas"),
    ("Palermo Soho", "Palermo"),
    ("Belgrano R.", "Belgrano R"),       # la más larga, no "Belgrano"
    ("Palerno", "Palermo"),              # tipeo
    ("Quilmes", None),
    ("Pa", None),                        # muy corto para "contenida en"
])
def test_emparejar_zona(pedida, zona):
    assert buscador.emparejar_zona(pedida, ZONAS) == zona


def test_zonas_aproximadas_e_inexistentes_se_avisan():
    r = buscador.validar_filtros({"zonas": ["Palermo Soho", "Quilmes", "palermo"]}, ZONAS)
    assert r["filtros"] == {"zonas": ["Palermo"]}
    assert any("Palermo Soho" in a for a in r["avisos"])
    assert any("Quilmes" in a for a in r["avisos"])


def test_si_ninguna_zona_existe_no_filtra_por_zona_y_lo_dice():
    r = buscador.validar_filtros({"zonas": "Quilmes", "precio_max": 100}, ZONAS)
    assert r["filtros"] == {"precio_max": 100.0}
    assert any("busco en todas" in a for a in r["avisos"])


def test_describir():
    texto = buscador.describir({"zonas": ["Palermo", "Belgrano"], "ambientes_min": 3,
                                "ambientes_max": 3, "precio_max": 150_000.0, "m2_min": 60.0})
    assert texto == "Palermo o Belgrano · 3 ambientes · hasta USD 150.000 · 60+ m²"


# --- interpretar, con un cliente falso ---------------------------------- #

CFG = Config({
    "search": {"currency": "USD"},
    "nl_search": {"enabled": True, "base_url": "http://localhost:11434/v1", "model": "qwen2.5"},
})


class ClienteFalso:
    """Responde `contenido` o lanza `error`; guarda lo que se le pidió."""

    def __init__(self, contenido=None, error=None):
        self.pedidos = []
        self.contenido, self.error = contenido, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.pedidos.append(kwargs)
        if self.error:
            raise self.error
        mensaje = SimpleNamespace(content=self.contenido)
        return SimpleNamespace(choices=[SimpleNamespace(message=mensaje)])


def test_interpretar_camino_feliz():
    cliente = ClienteFalso(json.dumps({"zonas": ["Palermo", "Belgrano"], "ambientes_min": 3,
                                       "ambientes_max": 3, "precio_max": 150000, "m2_min": 60}))
    r = buscador.interpretar("3 ambientes en Palermo o Belgrano, menos de 150 mil, más de 60 m²",
                             ZONAS, CFG, cliente=cliente)
    assert r["error"] is None
    assert r["filtros"]["zonas"] == ["Palermo", "Belgrano"]
    assert r["filtros"]["precio_max"] == 150_000
    # El modelo recibe la lista real de zonas y la moneda.
    prompt = cliente.pedidos[0]["messages"][0]["content"]
    assert "Cañitas" in prompt and "USD" in prompt
    assert cliente.pedidos[0]["response_format"] == {"type": "json_object"}


def test_json_envuelto_en_texto_se_rescata():
    cliente = ClienteFalso('Claro:\n```json\n{"precio_max": 90000}\n```')
    r = buscador.interpretar("hasta 90 mil", ZONAS, CFG, cliente=cliente)
    assert r["filtros"] == {"precio_max": 90_000.0}


@pytest.mark.parametrize("contenido", ["esto no es json", '{"precio_max": ', None, "[1, 2]", ""])
def test_json_invalido_no_rompe(contenido):
    r = buscador.interpretar("algo", ZONAS, CFG, cliente=ClienteFalso(contenido))
    assert r["filtros"] == {}
    assert "JSON" in r["error"]


@pytest.mark.parametrize("nombre, pista", [
    ("APITimeoutError", "tardó"),
    ("APIConnectionError", "localhost:11434"),
    ("AuthenticationError", "api_key"),
    ("NotFoundError", "qwen2.5"),
    ("ValueError", "ValueError"),
])
def test_errores_del_proveedor_son_mensajes(nombre, pista):
    error = type(nombre, (Exception,), {})("detalle")
    r = buscador.interpretar("algo", ZONAS, CFG, cliente=ClienteFalso(error=error))
    assert r["filtros"] == {}
    assert pista in r["error"]


def test_texto_vacio_no_llama_al_modelo():
    cliente = ClienteFalso("{}")
    assert buscador.interpretar("   ", ZONAS, CFG, cliente=cliente)["filtros"] == {}
    assert cliente.pedidos == []


def test_json_mode_se_puede_apagar():
    cfg = Config({**CFG, "nl_search": {**CFG["nl_search"], "json_mode": False}})
    cliente = ClienteFalso("{}")
    buscador.interpretar("algo", ZONAS, cfg, cliente=cliente)
    assert "response_format" not in cliente.pedidos[0]


# --- habilitado ---------------------------------------------------------- #

def test_habilitado(monkeypatch):
    monkeypatch.setattr(buscador, "_openai_instalado", lambda: True)
    assert buscador.habilitado(CFG)
    assert not buscador.habilitado(Config({}))
    assert not buscador.habilitado(Config({"nl_search": None}))
    assert not buscador.habilitado(Config({"nl_search": {**CFG["nl_search"], "enabled": False}}))
    assert not buscador.habilitado(Config({"nl_search": {"enabled": True, "model": "x"}}))

    monkeypatch.setattr(buscador, "_openai_instalado", lambda: False)
    assert not buscador.habilitado(CFG)
