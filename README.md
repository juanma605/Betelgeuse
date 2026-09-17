# inmobot

Scraping y análisis de avisos inmobiliarios, con todo parametrizado en `config.yaml`.

No junta avisos: construye el mercado. Precio por m² por zona, detección de
subvaluados, historial de precios y candidatos a duplicado entre agencias.

## Instalación

```bash
pip install httpx pandas pyyaml
```

## Uso

```bash
python -m inmobot scrape     # trae avisos y guarda un snapshot de precios
python -m inmobot analyze    # imprime el análisis en consola
python -m inmobot export     # además vuelca todo a data/reports/*.csv
```

Poné el `scrape` en un cron diario. El valor del proyecto crece con cada corrida:
el historial de precios es lo único que no podés conseguir en ningún portal.

```cron
0 7 * * * cd /ruta/inmobot && /usr/bin/python3 -m inmobot scrape >> data/cron.log 2>&1
```

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
| `alerts.telegram` | notificaciones por score de oportunidad |

Para cambiar de venta a alquiler, o de departamento a PH, tocás los IDs en
`sources.mercadolibre.api_filters`. Para venderle esto a un cliente, le cambiás
el YAML y nada más.

## Estado actual

- **MercadoLibre**: implementado. API pública, JSON estructurado, sin anti-bot.
- **Zonaprop / Argenprop**: el hueco está armado (`sources` en el config, registro
  en `cli.SOURCE_BUILDERS`) pero falta escribir el fetcher. Necesitan Playwright,
  headers reales y rate limit de 3-5 s. Son de la misma empresa y tienen
  protección anti-bot seria.

Para agregar una fuente: creá `inmobot/sources/tufuente.py` con una clase que
exponga `fetch(zone, search_cfg)` devolviendo dicts con las claves del esquema
(mirá `mercadolibre._map`), y registrala en `SOURCE_BUILDERS`.

## Cosas que tenés que verificar en la primera corrida real

No pude probar contra la API en vivo, así que revisá esto:

1. **Token de MercadoLibre.** La búsqueda pública históricamente no requería
   auth, pero ML fue restringiendo el acceso. Si ves errores 401/403, generá un
   token de app y ponelo en `sources.mercadolibre.access_token` (o exportá
   `ML_ACCESS_TOKEN` y usá `"env:ML_ACCESS_TOKEN"`).
2. **IDs de filtros.** Los valores de `OPERATION` y `PROPERTY_TYPE` son los
   históricos de MLA. Si no filtran bien, pegale a
   `/sites/MLA/search?category=MLA1459` y mirá `available_filters` en la
   respuesta para sacar los IDs vigentes.
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
3. **Bot de Telegram** leyendo `alerts.telegram` y disparando cuando aparece algo
   sobre `min_score`.
4. **Geocoding** de los avisos sin lat/long, para análisis por distancia a
   subte/parques en vez de por barrio.

## Escrúpulos

Rate limit conservador, `robots.txt` respetado, datos guardados localmente para
análisis propio. Scrapear despacio para analizar el mercado es terreno tranquilo;
redistribuir los avisos de un portal ya no lo es.
