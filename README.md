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
snapshots) y descarta el **contenido** de los portales: sin título original,
sin URL, sin el JSON crudo, y con las coordenadas redondeadas a ~100 m. El
título que se ve está reconstruido con los campos numéricos del propio aviso
("2 amb · 48 m² · Almagro"). Scrapear para analizar es una cosa; republicar
los avisos de otro es otra, y este repo no hace la segunda.

Es un dataset chico a propósito (una base de pocas corridas), así que
`analysis.min_comparables: 20` deja casi todos los grupos afuera y el análisis
lo dice en pantalla en vez de imprimir una tabla vacía. El umbral se queda como
está: una mediana de tres avisos no es un precio de mercado. Con más corridas
acumuladas la sección se enciende sola.

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
`sources.mercadolibre.category` (ver el mapeo de IDs en el comentario de
`config.yaml`). Para venderle esto a un cliente, le cambiás el YAML y nada
más.

## Estado actual

- **Zonaprop, Argenprop, Mudafy, Remax**: implementados con Playwright, rate
  limit de 4 s y el tope de páginas que fija el `robots.txt` de cada uno.
  Zonaprop y Argenprop cortan con verificación de Cloudflare cada tanto: el
  scraper la detecta, corta esa zona y avisa — no la esquiva.
- **MercadoLibre**: implementado y apagado. La búsqueda quedó detrás de un gate
  de certificación de app: con un `access_token` válido igual devuelve 403
  (`PolicyAgent`). El código y el config quedan listos por si se resuelve del
  otro lado.

Para agregar una fuente: creá `inmobot/sources/tufuente.py` con una clase que
exponga `fetch(zone, search_cfg)` devolviendo dicts con las claves del esquema
(mirá `mercadolibre._map`), y registrala en `SOURCE_BUILDERS`.

## Cosas que tenés que verificar en la primera corrida real

No pude probar contra la API en vivo, así que revisá esto:

1. **Token de MercadoLibre (confirmado, bloqueante).** La API pública ya no
   funciona sin auth: `/sites/MLA/search`, `/items/{id}` y prácticamente todo
   menos `/categories/*` devuelven 403 sin token, incluso sin ningún filtro.
   Hace falta generar un `access_token` vía OAuth (flujo `authorization_code`,
   no hay `client_credentials`: hay que crear una app en
   developers.mercadolibre.com.ar y loguearse una vez con una cuenta de ML) y
   ponerlo en `sources.mercadolibre.access_token` (o exportar `ML_ACCESS_TOKEN`
   y usar `"env:ML_ACCESS_TOKEN"`). El token expira a las 6 horas y el
   `refresh_token` es de un solo uso — para un cron hace falta guardar y rotar
   el refresh_token, no alcanza con pegar un token fijo.
2. **IDs de filtros (ya corregido).** `OPERATION`/`PROPERTY_TYPE` no existen
   más como filtros de atributo: ML pasó a modelar tipo de propiedad +
   operación como categoría anidada (ej. Departamentos `MLA1472` → Venta
   `MLA1474`). El config y `mercadolibre.py` ya usan `category` con la
   categoría hoja — ver el mapeo en el comentario de `config.yaml`.
3. **Nombres de atributos.** `ATTRIBUTE_MAP` en `mercadolibre.py` asume
   `COVERED_AREA`, `ROOMS`, `MAINTENANCE_FEE`, etc. Si vienen vacíos, imprimí
   `raw["attributes"]` de un aviso y ajustá el mapa.
4. **Duplicados.** El test offline los infla porque los títulos sintéticos son
   idénticos. Con títulos reales el filtro de similitud debería dejar pocos.
   Si siguen dando muchos, subí `title_similarity` en `duplicate_candidates`.

## Próximos pasos naturales

1. **Streamlit encima de la base.** ~50 líneas y tenés filtros, tabla y mapa.
   Es lo que convierte esto en algo mostrable a un cliente.
2. **Regresión en vez de mediana.** `find_undervalued` compara contra la mediana
   de zona/ambientes. Con 2.000+ avisos, una regresión sobre área, ambientes,
   antigüedad y barrio te va a dar un precio esperado mucho más fino.
3. **Alertas por mail** (`inmobot/alerts.py`, ya implementado) leyendo
   `alerts.email` y disparando cuando aparece algo sobre `min_score`. Solo
   falta completar host/usuario/contraseña SMTP y poner `enabled: true`.
4. **Geocoding** de los avisos sin lat/long, para análisis por distancia a
   subte/parques en vez de por barrio.

## Escrúpulos

Rate limit conservador, `robots.txt` respetado, datos guardados localmente para
análisis propio. Scrapear despacio para analizar el mercado es terreno tranquilo;
redistribuir los avisos de un portal ya no lo es.
