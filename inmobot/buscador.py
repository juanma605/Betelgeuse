"""Buscador en lenguaje natural: "3 ambientes en Palermo, menos de 150 mil".

El modelo NO escribe SQL. Lee la frase y devuelve un JSON con claves fijas
("zonas", "precio_max"...); de acá en adelante todo es Python común:

    interpretar()     -> llama al modelo y le pasa la respuesta a validar_filtros
    validar_filtros() -> se queda solo con claves conocidas y tipos correctos
    construir_sql()   -> arma el WHERE con placeholders `?`

La seguridad no depende de que el modelo se porte bien. Los nombres de
columna salen únicamente de CAMPOS, que está escrito acá; lo que viene del
modelo (o del usuario, a través del modelo) solo llega a SQLite como
parámetro. Un "Palermo'; DROP TABLE listings;--" es, para SQLite, el nombre
de una zona que no existe.

El proveedor es cualquiera que hable la API de OpenAI (Ollama local, Groq,
OpenRouter, la propia OpenAI...): base_url, modelo y clave salen de
`nl_search` en el config. El paquete `openai` es opcional y se importa recién
cuando hace falta, así que sin él todo lo demás anda igual.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import math
import unicodedata
from dataclasses import dataclass

# Una frase de búsqueda no necesita más. Corta también el caso de pegar un
# texto enorme por error y pagar los tokens.
MAX_CARACTERES = 500
TIMEOUT_DEFAULT_S = 20


@dataclass(frozen=True)
class Campo:
    expr: str               # expresión SQL: sale de acá, nunca del modelo
    op: str                 # ">=", "<=", "=" o "IN"
    tipo: type              # int, float, str o list (de str)
    columnas: tuple         # columnas de `listings` que la expresión necesita
    descripcion: str        # para el prompt y para mostrar lo que se entendió


CAMPOS: dict[str, Campo] = {
    "zonas": Campo("zone", "IN", list, ("zone",), "zonas (lista de nombres de la lista de abajo)"),
    "operacion": Campo("operation", "=", str, ("operation",), '"venta" o "alquiler"'),
    "ambientes_min": Campo("rooms", ">=", int, ("rooms",), "ambientes mínimo"),
    "ambientes_max": Campo("rooms", "<=", int, ("rooms",), "ambientes máximo"),
    "dormitorios_min": Campo("bedrooms", ">=", int, ("bedrooms",), "dormitorios mínimo"),
    "banos_min": Campo("bathrooms", ">=", int, ("bathrooms",), "baños mínimo"),
    "precio_min": Campo("price_norm", ">=", float, ("price_norm",), "precio mínimo"),
    "precio_max": Campo("price_norm", "<=", float, ("price_norm",), "precio máximo"),
    # La misma "área" que usa analyze.load_active: la cubierta, y si el aviso
    # no la publica, la total. Solo con cubiertos se perdería la mitad de la
    # base, casi todo Zonaprop incluido.
    "m2_min": Campo(
        "COALESCE(covered_area, total_area)", ">=", float,
        ("covered_area", "total_area"), "m² mínimo",
    ),
    # En pesos: las expensas se guardan como las publica el aviso, sin
    # convertir (van de miles a millones de ARS).
    "expensas_max": Campo("maintenance_fee", "<=", float, ("maintenance_fee",), "expensas máximas en pesos"),
}

OPERACIONES = ("venta", "alquiler")

# Pares mínimo/máximo: si vienen al revés, se dan vuelta.
RANGOS = (("ambientes_min", "ambientes_max"), ("precio_min", "precio_max"))


# --- configuración ------------------------------------------------------ #

def _openai_instalado() -> bool:
    return importlib.util.find_spec("openai") is not None


def habilitado(cfg) -> bool:
    """True solo si hay con qué buscar: sección completa, `enabled: true` y
    el paquete instalado. Cualquier otra cosa apaga el buscador sin error:
    quien clona el repo para ver el demo no tiene ni clave ni GPU."""
    conf = cfg.get_path("nl_search") or {}
    return bool(
        isinstance(conf, dict)
        and conf.get("enabled")
        and conf.get("base_url")
        and conf.get("model")
        and _openai_instalado()
    )


# --- validación (pura) --------------------------------------------------- #

def _plano(texto: str) -> str:
    """Minúsculas y sin tildes: "Cañitas" y "canitas" son la misma zona."""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower().strip()


def emparejar_zona(pedida: str, disponibles: list[str]) -> str | None:
    """La zona de la base que corresponde a lo que pidió el usuario, o None.

    En orden: igual salvo mayúsculas y tildes; una contenida en la otra
    ("Las Cañitas" -> "Cañitas", "Palermo Soho" -> "Palermo"), y ahí gana la
    más larga para que "Belgrano R." vaya a "Belgrano R" y no a "Belgrano";
    por último, parecido de letras para errores de tipeo ("Palerno").
    """
    plana = _plano(pedida)
    if not plana:
        return None
    por_plano = {_plano(z): z for z in disponibles}

    if plana in por_plano:
        return por_plano[plana]

    # Con menos de 4 letras "contenida en" matchea cualquier cosa.
    contenidas = [
        z for p, z in por_plano.items()
        if min(len(p), len(plana)) >= 4 and (p in plana or plana in p)
    ]
    if contenidas:
        return max(contenidas, key=len)

    parecidas = difflib.get_close_matches(plana, list(por_plano), n=1, cutoff=0.75)
    return por_plano[parecidas[0]] if parecidas else None


def _numero(valor, tipo: type):
    """El valor como `tipo`, o None si no es un número válido para eso.

    Los strings se rechazan a propósito, aunque parezcan números: "150.000"
    es 150 mil para un argentino y 150 para float(). El modelo tiene que
    devolver números JSON; si no, el filtro no se aplica.
    """
    # bool es subclase de int en Python: True pasaría como 1 ambiente.
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if not math.isfinite(valor) or valor < 0:
        return None
    if tipo is int:
        return int(valor) if float(valor).is_integer() else None
    return float(valor)


def validar_filtros(crudo, zonas_disponibles: list[str]) -> dict:
    """Lo que devolvió el modelo, reducido a filtros que se pueden aplicar.

    Devuelve {"filtros": {...}, "avisos": [...]}. Las claves desconocidas y
    los tipos incorrectos se descartan en silencio: son ruido del modelo, no
    algo que el usuario pueda corregir. Las zonas sí avisan, porque ahí el
    usuario puede reescribir la frase.
    """
    filtros: dict = {}
    avisos: list[str] = []
    if not isinstance(crudo, dict):
        return {"filtros": filtros, "avisos": avisos}

    for clave, campo in CAMPOS.items():
        if clave not in crudo or crudo[clave] is None:
            continue
        valor = crudo[clave]

        if clave == "zonas":
            # Un modelo chico a veces devuelve "Palermo" en vez de ["Palermo"].
            pedidas = [valor] if isinstance(valor, str) else valor
            if not isinstance(pedidas, list):
                continue
            zonas: list[str] = []
            for pedida in pedidas:
                if not isinstance(pedida, str) or not pedida.strip():
                    continue
                zona = emparejar_zona(pedida, zonas_disponibles)
                if zona is None:
                    avisos.append(f"No encontré la zona «{pedida}» en la base.")
                    continue
                if _plano(zona) != _plano(pedida):
                    avisos.append(f"Tomé «{pedida}» como {zona}.")
                if zona not in zonas:
                    zonas.append(zona)
            if zonas:
                filtros["zonas"] = zonas
            elif pedidas:
                # Mejor mostrar todo con el aviso a la vista que cero
                # resultados sin explicación.
                avisos.append(
                    "Ninguna zona pedida está en la base, así que busco en todas. "
                    "Zonas disponibles: " + ", ".join(sorted(zonas_disponibles)) + "."
                )

        elif clave == "operacion":
            if isinstance(valor, str) and _plano(valor) in OPERACIONES:
                filtros["operacion"] = _plano(valor)

        else:
            numero = _numero(valor, campo.tipo)
            if numero is not None:
                filtros[clave] = numero

    for minimo, maximo in RANGOS:
        if minimo in filtros and maximo in filtros and filtros[minimo] > filtros[maximo]:
            filtros[minimo], filtros[maximo] = filtros[maximo], filtros[minimo]

    return {"filtros": filtros, "avisos": avisos}


def quitar_sin_columna(filtros: dict, columnas: set[str]) -> tuple[dict, list[str]]:
    """Saca los filtros sobre columnas que esta base no tiene.

    El demo se generó antes de que existiera `operation` (son todas ventas):
    filtrar por eso ahí es un `no such column`, no un resultado vacío.
    """
    quedan, avisos = {}, []
    for clave, valor in filtros.items():
        campo = CAMPOS.get(clave)
        faltan = [c for c in campo.columnas if c not in columnas] if campo else []
        if faltan:
            avisos.append(f"Esta base no tiene la columna `{faltan[0]}`: ignoré «{clave}».")
            continue
        quedan[clave] = valor
    return quedan, avisos


# --- SQL (pura) ---------------------------------------------------------- #

def construir_sql(filtros: dict) -> tuple[str, list]:
    """SELECT de los ids de avisos activos que cumplen los filtros.

    Recorre CAMPOS, no `filtros`: una clave que no está en CAMPOS no puede
    llegar al SQL aunque alguien llame a esto sin validar antes. Los valores
    van siempre como `?`.
    """
    condiciones = ["active = 1"]
    params: list = []
    for clave, campo in CAMPOS.items():
        valor = filtros.get(clave)
        if valor is None:
            continue
        if campo.op == "IN":
            valores = [valor] if isinstance(valor, str) else list(valor)
            if not valores:
                continue
            condiciones.append(f"{campo.expr} IN ({', '.join('?' * len(valores))})")
            params.extend(valores)
        else:
            condiciones.append(f"{campo.expr} {campo.op} ?")
            params.append(valor)
    return "SELECT id FROM listings WHERE " + " AND ".join(condiciones), params


def describir(filtros: dict, moneda: str = "USD") -> str:
    """Los filtros en castellano, para que el usuario vea qué se entendió."""
    partes = []
    if "zonas" in filtros:
        partes.append(" o ".join(filtros["zonas"]))
    if "operacion" in filtros:
        partes.append(filtros["operacion"])

    amin, amax = filtros.get("ambientes_min"), filtros.get("ambientes_max")
    if amin is not None and amin == amax:
        partes.append("1 ambiente" if amin == 1 else f"{amin} ambientes")
    elif amin is not None and amax is not None:
        partes.append(f"{amin} a {amax} ambientes")
    elif amin is not None:
        partes.append(f"{amin}+ ambientes")
    elif amax is not None:
        partes.append(f"hasta {amax} ambientes")

    if "dormitorios_min" in filtros:
        partes.append(f"{filtros['dormitorios_min']}+ dormitorios")
    if "banos_min" in filtros:
        partes.append(f"{filtros['banos_min']}+ baños")
    if "precio_min" in filtros:
        partes.append(f"desde {moneda} {filtros['precio_min']:,.0f}".replace(",", "."))
    if "precio_max" in filtros:
        partes.append(f"hasta {moneda} {filtros['precio_max']:,.0f}".replace(",", "."))
    if "m2_min" in filtros:
        partes.append(f"{filtros['m2_min']:g}+ m²")
    if "expensas_max" in filtros:
        partes.append(f"expensas hasta ARS {filtros['expensas_max']:,.0f}".replace(",", "."))
    return " · ".join(partes)


# --- el modelo ----------------------------------------------------------- #

def _prompt(zonas_disponibles: list[str], moneda: str) -> str:
    claves = "\n".join(
        f'- "{clave}": {campo.descripcion}'
        + ("" if campo.tipo in (list, str) else f" ({'entero' if campo.tipo is int else 'número'})")
        for clave, campo in CAMPOS.items()
    )
    return f"""Convertís búsquedas de departamentos en Buenos Aires a un objeto JSON de filtros.

Claves posibles (usá solo estas, y solo las que el usuario pidió):
{claves}

Reglas:
- Los precios van en {moneda}, como número JSON sin separadores: "150 mil" es 150000.
- Las expensas van en pesos argentinos.
- "3 ambientes" es ambientes_min 3 y ambientes_max 3; "2 o 3 ambientes" es 2 y 3;
  "monoambiente" es 1 ambiente. "Más de 60 m²" es m2_min 60.
- Las zonas tienen que ser de esta lista, escritas igual: {", ".join(zonas_disponibles)}.
  Si el usuario nombra un sub-barrio (ej. "Palermo Soho"), usá la zona que lo contiene.
- Si la frase no pide nada que corresponda a una clave, devolvé {{}}.
- Respondé únicamente con el objeto JSON, sin texto alrededor."""


def _extraer_json(contenido: str):
    """El primer objeto JSON de la respuesta. Algunos modelos locales lo
    envuelven en texto o en ```json ... ``` aunque se les pida que no."""
    try:
        return json.loads(contenido)
    except (json.JSONDecodeError, TypeError):
        pass
    if not isinstance(contenido, str):
        return None
    inicio, fin = contenido.find("{"), contenido.rfind("}")
    if inicio == -1 or fin <= inicio:
        return None
    try:
        return json.loads(contenido[inicio:fin + 1])
    except json.JSONDecodeError:
        return None


def _mensaje_de_error(error: Exception, conf: dict) -> str:
    """Un error de red o del proveedor, explicado. Se mira el nombre de la
    clase para no tener que importar `openai` a nivel de módulo."""
    nombre = type(error).__name__
    if nombre == "APITimeoutError":
        return f"El modelo tardó más de {conf.get('timeout_s', TIMEOUT_DEFAULT_S)} s en responder."
    if nombre == "APIConnectionError":
        return f"No me pude conectar a {conf.get('base_url')}. ¿Está corriendo el servidor?"
    if nombre in ("AuthenticationError", "PermissionDeniedError"):
        return ("El proveedor rechazó la clave. Revisá `nl_search.api_key` y que la "
                "variable de entorno a la que apunta esté en el .env.")
    if nombre == "NotFoundError":
        return f"El proveedor no tiene el modelo «{conf.get('model')}» (o la base_url está mal)."
    if nombre == "RateLimitError":
        return "El proveedor dice que se excedió el límite de uso. Probá en un rato."
    return f"El modelo falló ({nombre})."


def interpretar(texto: str, zonas_disponibles: list[str], cfg, cliente=None) -> dict:
    """La frase del usuario convertida en filtros validados.

    Devuelve {"filtros", "avisos", "error"}. Nunca lanza: si el modelo no
    responde, tarda o devuelve cualquier cosa, `error` dice qué pasó y
    `filtros` queda vacío, así el dashboard sigue con los filtros manuales.

    `cliente` es para los tests: cualquier objeto con
    `.chat.completions.create(...)` como el de `openai`.
    """
    resultado = {"filtros": {}, "avisos": [], "error": None}
    texto = (texto or "").strip()[:MAX_CARACTERES]
    if not texto:
        return resultado

    conf = cfg.get_path("nl_search") or {}
    moneda = cfg.get_path("search.currency", "USD")

    try:
        if cliente is None:
            import openai

            cliente = openai.OpenAI(
                base_url=conf.get("base_url"),
                # Ollama no pide clave, pero el cliente no arranca sin una.
                api_key=conf.get("api_key") or "sin-clave",
                timeout=conf.get("timeout_s", TIMEOUT_DEFAULT_S),
                # Un reintento duplica la espera; mejor avisar y que el
                # usuario decida si vuelve a probar.
                max_retries=0,
            )
        extra = {}
        # Pide JSON a nivel de API. Casi todos lo soportan; al que no, se le
        # apaga con `json_mode: false` y queda solo la instrucción del prompt.
        if conf.get("json_mode", True):
            extra["response_format"] = {"type": "json_object"}
        respuesta = cliente.chat.completions.create(
            model=conf.get("model"),
            messages=[
                {"role": "system", "content": _prompt(zonas_disponibles, moneda)},
                {"role": "user", "content": texto},
            ],
            temperature=0,
            **extra,
        )
        contenido = respuesta.choices[0].message.content
    except ImportError:
        resultado["error"] = "Falta el paquete `openai` (pip install openai)."
        return resultado
    except Exception as error:  # noqa: BLE001 — cualquier falla es un mensaje, no un traceback
        resultado["error"] = _mensaje_de_error(error, conf)
        return resultado

    crudo = _extraer_json(contenido)
    if not isinstance(crudo, dict):
        resultado["error"] = "El modelo no devolvió un JSON válido. Probá reformular la frase."
        return resultado

    resultado.update(validar_filtros(crudo, zonas_disponibles))
    if not resultado["filtros"] and not resultado["avisos"]:
        resultado["avisos"].append("No entendí ningún filtro en la frase.")
    return resultado
