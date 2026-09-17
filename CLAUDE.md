# inmobot — contexto del proyecto

## Quién soy y qué busco

Estudiante de ingeniería industrial, segundo año. Sé Python, SQL, HTML/CSS/JS a
nivel intermedio: no soy experto, explicame las decisiones no obvias en vez de
solo escribir código. Trabajo a los saltos — días de muchas horas seguidas y días
de cero — así que prefiero avances que queden en un estado usable, no ramas a
medio terminar.

Esto tiene doble objetivo: me sirve a mí (estoy buscando departamento) y es mi
primer proyecto de portfolio para arrancar a freelancear con automatización y
datos.

## Qué es esto

Scraper + motor de análisis de avisos inmobiliarios, configurable por completo
desde `config.yaml`. La idea no es juntar avisos (eso ya lo hace cualquier
portal) sino construir el mercado: precio por m² por zona, detección de
subvaluados, historial de precios y candidatos a duplicado entre agencias.

El activo diferencial es el **historial de precios**: cada corrida guarda un
snapshot por aviso. Ningún portal muestra que un departamento bajó tres veces y
lleva 90 días publicado, y ese es el dato que sirve para negociar (y por el que
un cliente pagaría).

## Arquitectura

```
config.yaml              # TODO lo configurable — zonas, rangos, filtros, umbrales
inmobot/
  config.py              # carga, merge de defaults, resolución de "env:VAR"
  db.py                  # SQLite: listings (upsert) + price_snapshots (historial)
  normalize.py           # esquema común, conversión de moneda, filtros, fingerprint
  analyze.py             # precio/m², subvaluados, bajadas, stale, duplicados, score
  cli.py                 # scrape | analyze | export
  sources/
    mercadolibre.py      # implementado (API pública, sin anti-bot)
```

Principio de diseño: **nada hardcodeado**. Cambiar de "deptos en venta en
Caballito" a "PHs en alquiler en Quilmes" tiene que ser solo YAML. Si algo
necesita tocar código para cambiar de búsqueda, está mal diseñado.

## Estado

Probado offline con datos sintéticos: filtros, upsert, snapshots, subvaluados y
bajadas de precio funcionan. **Nunca se corrió contra la API real.**

## Tareas, en orden

### 1. Hacer que el scrape funcione de verdad (primero esto)

Corré `python -m inmobot scrape` y arreglá lo que rompa. Sospechas principales:

- **401/403**: ML fue restringiendo la búsqueda pública. Puede necesitar un token
  de app en `sources.mercadolibre.access_token`.
- **Filtros que no filtran**: los IDs de `OPERATION` (242075) y `PROPERTY_TYPE`
  (242060) son los históricos de MLA. Pegale a
  `/sites/MLA/search?category=MLA1459` y mirá `available_filters` para los
  vigentes.
- **Atributos vacíos**: `ATTRIBUTE_MAP` en `mercadolibre.py` asume
  `COVERED_AREA`, `ROOMS`, `MAINTENANCE_FEE`. Imprimí `raw["attributes"]` de un
  aviso real y ajustá el mapa.

No sigas a la 2 hasta tener avisos reales en la base.

### 2. Calibrar el análisis con datos reales

Corré `analyze` y revisá si los umbrales del config tienen sentido. En
particular `duplicate_candidates`: el fingerprint difuso da muchos falsos
positivos y el filtro de similitud de título (0.6) está sin calibrar contra
títulos reales.

### 3. Streamlit encima de la base

~50 líneas: filtros, tabla ordenable por score, mapa con lat/long. Es lo que
convierte esto en algo mostrable.

### 4. Zonaprop y Argenprop

El hueco ya está en `config.yaml` y en `cli.SOURCE_BUILDERS`. Falta el fetcher:
Playwright, headers reales, rate limit de 3-5 segundos. Son de la misma empresa
y tienen anti-bot serio. **Scrapeo lento y respetuoso de robots.txt** — no me
interesa ganar velocidad a cambio de un ban de IP.

Interfaz a implementar: una clase con `fetch(zone, search_cfg)` que devuelva
dicts con las claves del esquema (mirá `mercadolibre._map` como referencia),
registrada en `SOURCE_BUILDERS`.

### 5. Regresión en vez de mediana

`find_undervalued` compara contra la mediana de zona/ambientes. Con 2.000+ avisos
conviene una regresión sobre área, ambientes, antigüedad y barrio.

### 6. Alertas por Telegram

Leyendo `alerts.telegram` del config, disparando cuando aparece algo sobre
`min_score`.

## Cómo quiero que trabajes

- Corré el código antes de decir que anda. Si no lo pudiste probar, decilo.
- Cambios chicos y verificables, no refactors grandes.
- Si una decisión tiene un trade-off real, decímelo en una línea en vez de
  elegir en silencio.
- Los datos se guardan localmente para análisis propio. No redistribuir avisos.
