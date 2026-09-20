# inmobot

[![tests](https://github.com/juanma605/Betelgeuse/actions/workflows/tests.yml/badge.svg)](https://github.com/juanma605/Betelgeuse/actions/workflows/tests.yml)

Scraping y análisis de avisos inmobiliarios, con todo parametrizado en `config.yaml`.

No junta avisos: construye el mercado. Precio por m² por zona, detección de
subvaluados, historial de precios y candidatos a duplicado entre agencias.

## Probarlo en dos minutos, sin credenciales

El repo trae `data/demo.db`: avisos reales anonimizados, con su historial de
precios. No hace falta ni `.env` ni Playwright ni esperar un scrape.

```bash
pip install -r requirements.txt
python -m inmobot analyze --demo
streamlit run dashboard.py -- --demo
```

El demo conserva la **estructura** (precio, m², ambientes, zona, fechas,
snapshots) y el link a cada aviso, y descarta el **contenido** de los portales:
sin título original y sin el JSON crudo. El título que se ve está reconstruido
con los campos numéricos del propio aviso ("2 amb · 48 m² · Almagro"). Un link
es un puntero a una página pública; el título y la descripción son texto del
portal. Scrapear para analizar es una cosa; republicar los avisos de otro es
otra, y este repo no hace la segunda.

Los links son los originales, así que **algunos van a estar caídos**: los avisos
se dan de baja cuando se venden o se retiran, y el demo es una foto de un
momento.

Es un dataset chico (una base de pocas corridas), así que
`analysis.min_comparables: 20` deja casi todos los grupos afuera y el análisis
lo dice en pantalla en vez de imprimir una tabla vacía. Pesa además que las
medianas se calculan solo con avisos que publican m² cubiertos, y Zonaprop y
Mudafy no los muestran en el listado. El umbral se queda como está: una
mediana de tres avisos no es un precio de mercado. Con más corridas acumuladas
la sección se enciende sola.

Para regenerarlo desde tu propia base:

```bash
python -m inmobot demo-export                      # -> data/demo.db
python -m inmobot demo-export --limit 300 --out /tmp/chico.db
```

Es determinístico (dos corridas sobre la misma base dan el mismo archivo) y
muestrea estratificado por zona, conservando enteros los grupos
zona/ambientes: un demo sesgado a un solo barrio no mostraría nada.

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium   # solo para scrapear los portales sin API
```

## Uso

```bash
python -m inmobot scrape        # trae avisos y guarda un snapshot de precios
python -m inmobot analyze       # imprime el análisis en consola
python -m inmobot export        # además vuelca todo a data/reports/*.csv
python -m inmobot demo-export   # copia anonimizada de la base para el repo
```

Poné el `scrape` en un cron diario. El valor del proyecto crece con cada corrida:
el historial de precios es lo único que no podés conseguir en ningún portal.

```cron
0 7 * * * cd /ruta/inmobot && /usr/bin/python3 -m inmobot scrape >> data/cron.log 2>&1
```

## Ubicación: subte, hospitales y bomberos

El mapa muestra, además de los avisos, las estaciones de subte, los hospitales
con guardia y los cuarteles de bomberos, y cada capa se prende o apaga aparte.
Esa misma información entra en el score de oportunidad: el subte cerca suma, y
tener un hospital con guardia o un cuartel a menos de 200 m resta, porque son
las dos fuentes de sirenas a cualquier hora. Las distancias y los pesos se
configuran en `analysis.location` y `analysis.weights`.

Un aviso sin coordenadas **no se puntúa con cero** en esa parte: se lo mide con
las demás y ese peso se reparte. Si no, faltar un dato pesaría igual que estar
mal ubicado.

Remax y Mudafy publican coordenadas. Las otras tres publican la dirección, y
esa dirección se convierte en un punto con el normalizador del GCBA después de
cada scrape (`geocoding` en el config). Cada dirección se consulta una sola vez
y queda cacheada en la tabla `geocode_cache`, así que el atraso se limpia en
pocas corridas. Si el normalizador devuelve más de una opción, o un punto fuera
de CABA, el aviso queda sin ubicación: mejor eso que ponerlo en la cuadra
equivocada.

Los lugares están en `inmobot/lugares.json`, commiteado para que el análisis y
el mapa funcionen sin red. Para actualizarlo:

```bash
python scripts/bajar_lugares.py
```

Fuentes: subtes y bomberos del [portal de datos abiertos de la Ciudad de
Buenos Aires](https://data.buenosaires.gob.ar) (CC-BY 2.5 AR); hospitales con
guardia de [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL).
Los hospitales no salen del dataset oficial porque publica las coordenadas en
el sistema propio de la Ciudad (EPSG:9498), y convertirlas sin una librería de
proyecciones es adivinar.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Corren sin red, sin credenciales y sin Playwright: unos segundos. No buscan
cobertura, buscan las regresiones que duelen.

El grupo que más vale es `tests/test_sources.py`: corre el `_map()` de cada
fuente contra una tarjeta real del portal guardada en `tests/fixtures/`
(anonimizada, igual que el demo). Cuando un portal renombra una clase CSS el
scraper no explota — sigue corriendo y llena la base de nulls, que es la forma
en que estos proyectos se rompen sin que nadie se entere. Ya pasó: Argenprop
sacó el elemento con el barrio y el fixture lo destapó.

## Qué es configurable

Todo lo que cambia entre búsquedas está en `config.yaml`, no en el código:

| Sección | Qué controla |
|---|---|
| `search.zones` | barrios o partidos a rastrear |
| `search.price_min/max`, `search.currency` | rango y moneda de comparación |
| `search.filters` | m², ambientes, dormitorios, expensas, antigüedad, fotos |
| `search.fx_rates` | cotización para normalizar avisos en pesos |
| `sources.*` | qué portales usar, rate limit, filtros nativos de cada API |
| `dedup` | tolerancias de área y precio para el fingerprint |
| `analysis` | umbral de subvaluado, mínimo de comparables, días de antigüedad, recorte de outliers |
| `alerts.email` | notificaciones por mail según score de oportunidad |

Para cambiar de venta a alquiler, o de departamento a PH, tocás
`property_slug` y `operation_slug` de cada fuente en `config.yaml`. Para
venderle esto a un cliente, le cambiás el YAML y nada más.

## Estado actual

| Fuente | Cómo se lee | Tope por zona y corrida | m² cubiertos | Ubicación |
|---|---|---|---|---|
| Zonaprop | Playwright, 8 s entre páginas | 5 páginas (`robots.txt`) | no, solo totales | no |
| Argenprop | Playwright, 8 s entre páginas | 3 páginas (`robots.txt`) | sí | no |
| Mudafy | Playwright | ~25 avisos (no pagina) | no dice (se toma como total) | sí, ~100 m |
| Remax | Playwright, 6 s entre páginas | 3 páginas (tope propio) | sí | sí |
| MercadoLibre | HTTP simple, 8 s entre zonas | 1 página (`robots.txt`) | sí | no |

Zonaprop y Argenprop cortan con verificación de Cloudflare cada tanto: el
scraper la detecta, corta esa zona sin dar de baja sus avisos, y avisa — no la
esquiva.

MercadoLibre se lee desde el sitio público y no desde la API: la API cerró la
búsqueda a apps no certificadas (403 `PolicyAgent` aunque el token sea válido)
y la certificación exige 30 usuarios activos y 300 publicaciones. Se buscan
solo "propiedades individuales": sin ese filtro la primera página son casi
todos emprendimientos.

Las medianas de precio/m² salen solo de avisos con **m² cubiertos**. Los que
publican solo totales se evalúan igual pero salen marcados con `*`: su precio
por m² sale más bajo de lo real (un PH con patio, un balcón grande).

Para agregar una fuente: creá `inmobot/sources/tufuente.py` con una clase que
exponga `fetch(zone, search_cfg)` devolviendo dicts con las claves del esquema
(mirá cualquiera de las de `inmobot/sources/`), registrala en
`SOURCE_BUILDERS` y sumale un test de contrato con una tarjeta real en
`tests/fixtures/`.

## Próximos pasos naturales

1. **Ubicación para Zonaprop y Argenprop.** Son ~55% de los avisos y hoy no
   aparecen en el mapa: la ubicación solo está en la ficha de cada aviso.
2. **Regresión en vez de mediana.** `find_undervalued` compara contra la mediana
   de zona/ambientes. Con 2.000+ avisos, una regresión sobre área, ambientes,
   antigüedad y barrio da un precio esperado mucho más fino.

## Escrúpulos

Rate limit conservador, `robots.txt` respetado, datos guardados localmente para
análisis propio. Scrapear despacio para analizar el mercado es terreno tranquilo;
redistribuir los avisos de un portal ya no lo es.
