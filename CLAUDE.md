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
    mercadolibre.py      # sitio público por HTTP (la API cerró la búsqueda)
    zonaprop.py, argenprop.py, remax.py   # Playwright
    mudafy.py              # HTTP: sitemap de fichas + tarjetas del listado
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

## Pendientes para más adelante

### Ubicación para Zonaprop

Remax y Mudafy traen coordenadas en el listado; MercadoLibre y Argenprop traen
la dirección y se geocodifica (`inmobot/geocode.py`). Zonaprop no trae
ninguna de las dos: en la tarjeta solo está el barrio, y la dirección aparece
en la ficha de cada aviso, lo que implica cientos de requests extra por corrida
contra un Cloudflare que ya corta en los listados. Son 903 de los 1.947 avisos
activos y son los únicos que quedan sin ubicación.

Antes de implementar: revisar el `robots.txt` de las fichas y medir cuántas se
pueden pedir sin disparar la verificación. Otra opción es buscar la dirección
en el texto de la descripción, que a veces la menciona, pero eso es adivinar.

### Argenprop y Cloudflare

Cloudflare venía cortando casi todos sus listados (325 verificaciones el
22/09, 82 el 23, 50+ el 24 en 20 minutos). Estuvo en pausa el 24 y el
25/09 para que la IP se enfríe y se reactivó para la corrida del 26. Deja
de pedir después de 3 verificaciones seguidas (`max_challenges_in_a_row`),
así que si sigue marcada el costo es de 3 páginas por corrida. Mirar el
log del 26: si frena enseguida, la IP sigue marcada.

El camino a cubrirlo de verdad es su sitemap de fichas
(`sitemap-ficha-venta-caba`, 3 partes .xml.gz): lista ~36.500 deptos en
venta en nuestras zonas (teníamos 696), con barrio y ambientes en la URL y
`lastmod` real, y cubre el 99% de lo que teníamos activo. El sitemap se
baja por HTTP sin cortes; las fichas no: a 8 s cortó a la séptima con un
202 vacío (desafío, no página). Para retomar:

1. Con la IP fría, pedir unas pocas páginas a mano para ver si sigue
   marcada.
2. Medir fichas más lento (20-30 s) y lejos del horario del cron, contando
   el 202 vacío como desafío.
3. Recién con ese dato, diseñar la carga inicial (son muchas fichas) y el
   refresco por `lastmod`.

Aunque las fichas no anden, el sitemap solo ya sirve: si un aviso no está,
se dio de baja (bajas exactas, como Remax) y da el denominador por zona.

### Agregar una zona no es solo sumarla a la lista

Cada portal nombra los barrios distinto y ninguno devuelve 404 cuando no
reconoce el slug: devuelven otra búsqueda. Agregando "Cañitas" a
`search.zones`, MercadoLibre entendió bien (45 avisos de CABA), Zonaprop
trajo uno de Córdoba y Remax devolvió los 22.864 del país entero — 148
avisos de Allen, San Jerónimo y Mar del Plata entraron a la base con
precio, m² y fotos perfectamente válidos.

Hay dos redes ahora: `search.bbox` descarta lo que cae fuera del recuadro,
y Remax corta la zona si los `geoLabel` no mencionan el barrio pedido. Las
dos son redes, no reemplazan mirar el primer scrape de una zona nueva.

Un sub-barrio que el portal no busca por separado igual puede llegar: Remax
mete "Las Cañitas" y "Belgrano R" adentro de Palermo y Belgrano, y Mudafy no
tiene listado para ninguno de los dos (404) pero sus fichas los nombran. En
las dos fuentes la zona sale del barrio que declara cada aviso
(`_sitemap.zona_de_barrio`), no de la búsqueda que lo trajo.

### El alquiler estimado por edificio está mal en un tercio de los casos

`alquiler_mes` y `rinde_anual_pct` (dashboard, y `yields`) salen de tomar el
USD/m² al que se alquila el edificio y multiplicarlo por los m² del aviso de
venta. Medido contra los alquileres reales de cada edificio: **77 de 213
estimaciones (36%) caen muy lejos del rango que ese edificio realmente
alquila**. Ejemplos:

    Colegiales, 213 m²  ->  decimos 3.905   el edificio alquila 733 a 750
    Almagro,    161 m²  ->  decimos 3.897   el edificio alquila 1.000 a 1.033

La causa NO es que el m² de alquiler no sea lineal: se midió y lo es (14-15
USD/m² parejo de 15 a 300 m²). El problema es **extrapolar fuera del rango
observado**. Ese edificio de Colegiales solo publica alquileres de
departamentos chicos; su USD/m² no dice nada sobre uno de 213 m², y con 1 a
3 alquileres por edificio (la mediana es 2) eso pasa casi siempre.

Salidas posibles, para cuando se retome:

- Estimar solo cuando el metraje del aviso de venta cae dentro del rango de
  metrajes que ese edificio alquila, y dejarlo vacío si no.
- Mostrar el rango real (`733–750`) en vez de un número inventado.
- Ajustar por tipología: emparejar contra alquileres de ambientes parecidos
  en vez de escalar por m².

Mientras tanto la columna queda, con la advertencia a la vista en el
dashboard. Se decidió no darla de baja.

### El dashboard no recarga lo que importa

Streamlit recarga `dashboard.py` cuando cambia, pero no los módulos de
`inmobot/` que ese archivo importa: quedan en memoria como estaban cuando
arrancó el proceso. Si tocás algo adentro de `inmobot/`, avisame que hay que
reiniciar el dashboard; si solo cambia `dashboard.py`, con refrescar alcanza.

Ya pasó dos veces y las dos se vieron como un error que no existía:
`KeyError: 'comisarias'` por una lista que ya no estaba, y
`KeyError: 'alquiler_url'` por una columna recién agregada. El traceback
apunta a código viejo, así que los números de línea no coinciden con el
archivo — esa es la pista.

## Cómo quiero que trabajes

- Corré el código antes de decir que anda. Si no lo pudiste probar, decilo.
- Cambios chicos y verificables, no refactors grandes.
- Si una decisión tiene un trade-off real, decímelo en una línea en vez de
  elegir en silencio.
- Los datos se guardan localmente para análisis propio. No redistribuir avisos.
