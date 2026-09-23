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

## Cuánto rinde comprar

```bash
python -m inmobot scrape --config config-alquiler.yaml    # -> data/rentals.db
python -m inmobot geocode --config config-alquiler.yaml   # resuelve direcciones
python -m inmobot yields
```

El paso del medio importa más de lo que parece: el cruce solo puede emparejar
avisos con la dirección ya convertida en punto, y una base recién armada
queda con miles sin resolver. `geocode` las resuelve todas de una, sin
volver a pedirle nada a los portales.

La pregunta no es cuánto sale un departamento sino qué relación hay entre lo
que sale y lo que rinde. Un 2 ambientes de 150.000 USD que se alquila a 600
por mes rinde 4,8% anual; el mismo precio con un alquiler de 400 rinde 3,2%.
Ningún portal cruza las dos cosas, porque cada aviso vive en su propia
búsqueda.

Los alquileres van a **su propia base**, con su propio config: una corrida no
toca la otra, y conviene correrlas en horarios distintos.

Dos avisos son del mismo edificio cuando su dirección geocodifica al mismo
punto. No se usa proximidad: con 10.000 avisos en diez barrios, dos
departamentos a 40 metros son vecinos casi con seguridad y no la misma torre,
y las coordenadas que publican Remax y Mudafy son aproximadas. Eso deja el
cruce sobre la parte de la base que publica calle y altura.

La comparación es **por m²** y no por tipología: casi nunca hay venta y
alquiler de la misma cantidad de ambientes en el mismo edificio, así que un
monoambiente en alquiler sirve para medir un 3 ambientes en venta.

> **Esta parte todavía no es confiable.** El alquiler estimado sale de
> escalar por m² lo que alquila el edificio, y medido contra los alquileres
> reales, un 36% cae muy lejos de lo que ese edificio alquila de verdad: uno
> que solo publica monoambientes no dice nada sobre un departamento de 200
> m². El detalle y las salidas posibles están en `CLAUDE.md`.

El dashboard trae las mismas dos columnas por aviso: `alquiler_mes` y
`rinde_anual_pct`, vacías cuando no hay ningún alquiler de ese edificio. No
son "el alquiler de ese departamento" —eso no existe, el aviso es de venta—
sino lo que rendiría ese metraje a los USD/m² a los que se alquila el
edificio. Por eso un 3 ambientes y un monoambiente de la misma torre
muestran números distintos saliendo del mismo dato.

El rendimiento es **bruto** — alquiler anual sobre precio de venta, sin
descontar expensas, impuestos, vacancia ni comisión. Sirve para comparar
edificios entre sí; el número de bolsillo es más bajo. Mirá `venta_avisos` y
`alquiler_avisos` antes de creerle a una fila: un edificio con uno de cada
lado es el capricho de dos publicaciones.

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

El dashboard filtra por lo mismo: una distancia máxima al subte y una mínima
a hospitales y cuarteles. Los avisos sin ubicación siguen apareciendo aunque
se filtre por distancia —no sabemos dónde están, que es distinto de saber que
están lejos— y para sacarlos hay un checkbox aparte, que es una decisión
propia y no un efecto colateral de mover un slider.

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

## Buscar en lenguaje natural (opcional)

Con esto activado, el dashboard suma un campo arriba de los filtros donde se
escribe algo como *"3 ambientes en Palermo o Belgrano, menos de 150 mil
dólares, con más de 60 m²"*. Debajo muestra lo que entendió
(`Palermo o Belgrano · 3 ambientes · hasta USD 150.000 · 60+ m²`), así se ve
la interpretación y se puede corregir la frase. Los filtros manuales siguen
funcionando sobre el resultado.

**El modelo no escribe SQL.** Devuelve un JSON con claves fijas (zonas,
operación, ambientes, dormitorios, baños, precio, m², expensas). Python se
queda con las claves conocidas que tengan el tipo correcto y arma la consulta
con parámetros (`inmobot/buscador.py`), así que lo que escriba el usuario o
el modelo nunca pasa a formar parte del SQL. Al modelo se le pasa la lista
real de zonas de la base, y además del lado de Python se hace un emparejamiento
aproximado ("Las Cañitas" → Cañitas, "Palerno" → Palermo). Lo que no se
encuentra se avisa en pantalla. Los m² son los cubiertos, y si el aviso no
los publica, los totales, igual que en el resto del análisis.

Es **opcional**: si falta la sección `nl_search`, tiene `enabled: false`, o
no está instalado el paquete `openai`, el dashboard es el de siempre. Si el
modelo tarda, no responde o devuelve cualquier cosa, aparece un mensaje y se
sigue con los filtros manuales.

Sirve cualquier proveedor compatible con la API de OpenAI, y cambiar de uno a
otro es solo tocar el YAML:

```bash
pip install openai
```

Con **Ollama**, local y gratis ([ollama.com](https://ollama.com); hay que
bajar el modelo una vez con `ollama pull qwen2.5:7b`):

```yaml
nl_search:
  enabled: true
  base_url: "http://localhost:11434/v1"
  model: "qwen2.5:7b"
  timeout_s: 30        # en CPU, la primera respuesta tarda
```

Con un **proveedor en la nube** (el ejemplo es Groq; OpenAI, OpenRouter y
otros funcionan igual). La clave va en el `.env` como `NL_SEARCH_API_KEY=...`,
nunca en el YAML:

```yaml
nl_search:
  enabled: true
  base_url: "https://api.groq.com/openai/v1"
  model: "openai/gpt-oss-120b"
  api_key: "env:NL_SEARCH_API_KEY"
```

Si el proveedor rechaza el pedido de respuesta en JSON (`response_format`),
agregá `json_mode: false`: la instrucción queda en el prompt y la respuesta
se valida igual.

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

## Los portales se recorren en paralelo

Cada portal corre en su propio hilo. No cambia el ritmo con que se le pide a
ninguno —cada fuente mantiene su rate limit— pero el reloj total pasa de ser
la suma de los cinco a ser el del más lento. Esperarlos en fila no los
cuidaba: solo nos hacía esperar. Medido sobre tres fuentes y dos zonas: 55 s
en fila contra 32 s en paralelo.

Los hilos solo leen. La base la escribe el hilo principal a medida que cada
fuente termina, que es como venía siendo: SQLite con varios escritores es un
problema que no hace falta tener. Y una fuente que explota se registra y se
sigue con las demás, porque en paralelo una excepción suelta se llevaría la
corrida entera en vez de un portal.

Se configura con `scrape.parallel_sources`. Cada hilo levanta su propio
Chromium, así que subirlo cuesta memoria; con 1 vuelve a correr todo en fila.

## El sitemap como fuente de búsquedas

Armando la URL a mano solo se llega a `/departamentos/venta/palermo`, y como
el `robots.txt` de Argenprop corta en la página 3, de ahí salen 60 avisos de
los ~10.000 que el portal tiene en Palermo.

Pero el mismo `robots.txt` declara sus sitemaps, y el de listados de venta en
CABA trae 859 URLs: el portal indexa también `palermo-chico`,
`palermo-hollywood`, `palermo-soho`, `palermo-nuevo` y `palermo-viejo`, más
las variantes por tipo (`duplex`, `loft`) y por tamaño (`monoambiente`). Cada
una es otra búsqueda, con sus propias 3 páginas.

No es una vuelta astuta: un sitemap es la lista de URLs que el sitio pide que
se crawleen, publicada al lado de sus `Disallow`. El tope de páginas se sigue
respetando en cada una.

El solapamiento entre sub-barrios es bajo — Palermo Hollywood aportó 52
avisos nuevos sobre 60 — porque cada búsqueda es una ventana distinta sobre
el mismo inventario grande.

Las rutas se agrupan bajo la zona del config que les corresponde, y se la
queda la que matchea más específico: `belgrano-r` es de "Belgrano R" y no de
"Belgrano", porque si no sus avisos quedarían archivados en el barrio
equivocado y contaminarían las medianas de los dos.

Pedirlas todas de una no funciona: las 18 de Palermo son 54 páginas
seguidas y Cloudflare corta mucho antes — probado, pasaron 8. Así que se
rotan con `max_searches_per_zone`: la búsqueda del barrio entero va siempre y
el resto se reparte entre corridas, eligiendo el tramo por el día del año.
En unos días se recorren todas igual, sin castigar al portal. Con el cron
diario, las 18 de Palermo se cubren en cuatro días.

Se configura con `sources.argenprop.sitemap`. El nombre lleva la operación y
la región, así que para alquiler o para GBA hay que cambiarlo; con `null` se
vuelve a una sola búsqueda por zona. Si el sitemap no responde, el scrape
sigue con esa búsqueda única en vez de cortar.

Zonaprop hace lo mismo y en más escala, solo que hay que ir a buscarlo: sus
sitemaps dan 403 por HTTP directo y también por la API de red del navegador
—el WAF distingue la navegación de la petición de fondo— así que se navega al
archivo y se lo toma como descarga, porque vienen gzipeados. De ahí salen 325
búsquedas para las mismas diez zonas, con sub-barrios (`bajo-palermo`,
`botanico-palermo`, `barrio-parque-palermo`), ambientes y atributos
(`-con-balcon`, `-con-apto-credito`). **Palermo pasó de 111 avisos a 610.**

Dos cuidados ahí. El sitemap de Zonaprop es nacional, y `belgrano-rosario` es
el Belgrano de Rosario: por eso la zona tiene que caer al *final* del lugar y
no en cualquier parte, con una excepción para el sub-barrio seguido de su
barrio padre (`belgrano-r-belgrano`). Y el sitemap lista URLs que el propio
`robots.txt` no deja pedir —todos los `-orden-*` menos
`-orden-precio-ascendente`— así que se filtran: manda el `robots.txt`.

## Lo que no es una oferta

Ordenar la base por precio/m² ascendente debería mostrar las oportunidades.
Mostraba otra cosa: "Compramos propiedades en CABA" a 20 USD/m² (una
inmobiliaria que compra, no que vende), "Propiedad ficticia no consultar" a
75, y emprendimientos publicando el anticipo en lugar del precio de la
unidad, a 142 y 179.

`search.min_price_per_m2` pone un piso grosero —200 USD/m², contra una
mediana de CABA de ~2.700— que descarta disparates sin rozar ninguna
oportunidad real. Un aviso sin superficie no se filtra: no hay con qué
dividir, y adivinar es peor.

El pozo se detecta además mirando la URL entera y no solo la ruta
`/emprendimiento/`: Zonaprop arma el slug con el título original del aviso,
así que ahí queda la palabra que el título mostrado perdió. "Malva Rivera |
Viví donde el diseño hace la diferencia" no dice nada, pero su URL termina
en `...-departamenos-venta-pozo-...`.

Sigue habiendo avisos que publican un precio que no es el de la propiedad
("consultar precio" con un 22.222 de relleno). No se los marca como pozo
porque no lo son, y el piso de precio/m² solo los agarra si el número de
relleno es lo bastante bajo.

## Dar de baja un aviso: solo si vimos toda la zona

Un aviso que deja de aparecer suele haberse vendido, y darlo de baja es lo
que mantiene la base limpia. Pero eso vale solo si llegamos al final de la
lista. Remax es la única fuente que puede: su `robots.txt` no pone tope y se
pagina hasta que la lista vuelve vacía. Las otras cuatro tienen tope — 5
páginas en Zonaprop, 3 en Argenprop, 1 en MercadoLibre, ninguna en Mudafy —
y de los 5.793 avisos que ML tiene en Palermo vemos 48.

Ahí "no apareció" no significa "se vendió", significa "quedó fuera de las
páginas que nos dejan mirar", y bajarlo mata avisos vivos. Esas zonas se
marcan incompletas y no se da de baja nada en ellas.

Pasó de verdad: el 21/09, al arreglar los cortes de Cloudflare, las zonas de
Zonaprop dejaron de estar incompletas por primera vez y se dieron de baja
815 avisos de una corrida. Los cuatro que se revisaron a mano seguían
publicados. El bug existía desde siempre, tapado por los bloqueos.

Pero tampoco puede ser que un vendido quede activo para siempre, así que
ahí la baja es paciente: se cuenta cuántas corridas seguidas no apareció el
aviso y recién a las `storage.max_missed_runs` (7 por defecto, o sea una
semana de cron diario) se lo da de baja. El contador se reinicia apenas
vuelve a verse, así que un listado que rota no lo acumula nunca. Con 0 no se
da de baja nada en esas fuentes.

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
| Zonaprop | Playwright, 8 s entre páginas | 5 páginas + 1 reordenada (`robots.txt`) | no, solo totales | sí, por dirección |
| Argenprop | Playwright, 8 s entre páginas | 3 páginas + 1 reordenada (`robots.txt`) | sí | sí, por dirección |
| Mudafy | Playwright | ~25 avisos (no pagina) | no dice (se toma como total) | sí, ~100 m |
| Remax | Playwright, 6 s entre páginas | hasta agotar la zona (tope propio) | sí | sí |
| MercadoLibre | HTTP simple, 8 s entre zonas | 1 página (`robots.txt`) | sí | sí, por dirección |

El tope de cada fuente sale de su `robots.txt`, no de lo que aguanta el
sitio. Zonaprop y Argenprop prohíben paginar más allá de 5 y 3, y prohíben
reordenar la búsqueda salvo por precio ascendente, que habilitan con un
`Allow` puntual: esa pasada extra trae los más baratos de la zona, que es
otra lista y no las mismas tarjetas dadas vuelta. MercadoLibre prohíbe las
dos cosas — `Disallow: /*_Desde_` y `Disallow: *_PriceRange_` — así que
queda en una página (~48 avisos) de las 5.793 que tiene en Palermo; la
única forma de crecer ahí es agregar más barrios a `search.zones`.

Remax es la excepción: su `robots.txt` son cuatro líneas y no impone ni
tope de páginas ni `Crawl-delay`, así que se lo agota. Es además la fuente
más completa: publica m² cubiertos, coordenadas y barrio.

De Remax no se lee el HTML sino el `#ng-state`, el JSON que Angular deja
en la página para no volver a pedir los resultados. Llega con el HTML
inicial, así que no hay que esperar a que se dibujen las tarjetas, y el
`pageSize` de la URL se reenvía a su API: **100 avisos en 2,6 s contra 24
en 7,8 s**. Palermo entero pasó de 62 páginas y 11,9 minutos a 16 páginas y
2,4 minutos — cuatro veces menos pedidos al sitio, además de más rápido.

Zonaprop y Argenprop cortan con verificación de Cloudflare: el scraper la
detecta, corta esa zona sin dar de baja sus avisos, y avisa — no la resuelve
ni la falsifica.

Lo que sí hace es no dispararla: Cloudflare marca la **sesión**, no la IP, y
reusando la misma pestaña cortaba en la segunda navegación, dejándonos con 1
de las 5 páginas que el `robots.txt` autoriza. Abriendo una pestaña limpia
por página entran las cinco — en Palermo, 25 avisos pasan a 111. El rate
limit y el tope de páginas son los mismos; lo único que cambia es que no
arrastramos la sesión anterior. Se apaga con `new_session_per_page: false`.

MercadoLibre se lee desde el sitio público y no desde la API: la API cerró la
búsqueda a apps no certificadas (403 `PolicyAgent` aunque el token sea válido)
y la certificación exige 30 usuarios activos y 300 publicaciones. Se buscan
solo "propiedades individuales": sin ese filtro la primera página son casi
todos emprendimientos.

Los emprendimientos se descartan en MercadoLibre y en Zonaprop, que los
mezclan con los avisos sueltos en el mismo listado. No es purismo: la tarjeta
de un edificio trae el precio de la unidad más chica ("desde USD 148.680")
junto al rango de superficies ("48 a 148 m² tot."), y cruzar las dos puntas
da 1.005 USD/m² donde la mediana ronda los 2.700 — un 66% de descuento
fabricado, entrando justo a la lista de subvaluados.

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
