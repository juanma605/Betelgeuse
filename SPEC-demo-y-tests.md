# Tarea: dataset de demo + tests

Dos tareas independientes. Hacelas en ese orden y con commits separados.

Contexto: el repo es público y su función es portfolio. Alguien que lo evalúa
tiene cinco minutos, no tiene credenciales de MercadoLibre ni va a instalar
Playwright. Hoy el proyecto es imposible de probar desde afuera.

Estado de los datos: **una sola corrida, ~150 avisos**. O sea que hay avisos
pero no hay historial de precios todavía. Eso condiciona el diseño y es el punto
central de la tarea A.

---

# TAREA A — Dataset de demo

## Objetivo

Que `python -m inmobot analyze --demo` y el dashboard funcionen sin red, sin
credenciales y sin Playwright, contra un `data/demo.db` commiteado al repo.

## A.1 — Anonimización (requisito, no opcional)

El repo es público. Republicar avisos de los portales con título, URL y
dirección es redistribuir su contenido, y no hace falta para demostrar el
análisis. El demo conserva **estructura**, no **contenido**.

Se conserva tal cual:
`id` (reemplazado, ver abajo), `source`, `zone`, `neighborhood`, `city`,
`price`, `currency`, `price_norm`, `maintenance_fee`, `covered_area`,
`total_area`, `rooms`, `bedrooms`, `bathrooms`, `age_years`, `photo_count`,
`fingerprint`, `first_seen`, `last_seen`, `active`, y **toda la tabla
`price_snapshots`**.

Se transforma o se elimina:
- `url` → `NULL`.
- `title` → generado sintéticamente a partir de los campos estructurales
  (`"Departamento 2 amb · 48 m² · Almagro"`). Ojo: `duplicate_candidates` usa
  similitud de títulos, así que un título sintético derivado de los mismos
  campos va a dar similitud ~1.0 y va a inflar los duplicados. Por eso ver A.4.
- `raw` → `NULL` (es el JSON crudo del portal, es lo más sensible de todo).
- `latitude` / `longitude` → redondear a 3 decimales (~100 m). Suficiente para
  que el mapa se vea poblado, insuficiente para identificar la unidad.
- `id` y `source_id` → reemplazar por `demo:0001`, `demo:0002`… manteniendo la
  correspondencia con `price_snapshots.listing_id`. **Verificá que la relación
  no se rompa**: si se rompe, el historial de precios del demo queda huérfano.

Agregá al README una línea diciendo que el demo está anonimizado y para qué.

## A.2 — Comando de exportación

```
python -m inmobot demo-export [--limit 500] [--out data/demo.db]
```

Lee la base real (`storage.path` del config), aplica A.1, y escribe una SQLite
nueva con el mismo esquema. Reproducible: correrlo dos veces sobre la misma base
da el mismo resultado (seed fijo si hay algo aleatorio).

Muestreo: si hay más avisos que `--limit`, elegí de forma **estratificada por
zona**, no los primeros N. Con 150 avisos hoy entran todos, pero cuando haya
5.000 un demo sesgado a un solo barrio no muestra nada — y `zone_stats` exige
`min_comparables` por grupo, así que un muestreo plano puede dejar todas las
zonas por debajo del mínimo y producir un demo donde el análisis no devuelve
nada. Priorizá conservar grupos zona/ambientes completos por encima de llegar
al límite exacto.

## A.3 — Flag `--demo`

En `analyze`, `export` y el dashboard: usa `data/demo.db` ignorando
`storage.path`. Una sola línea en la resolución del path, no un camino de código
paralelo. El demo tiene que ejercitar exactamente el mismo código que la base
real, si no deja de ser una demostración.

## A.4 — Degradar bien con poca historia (lo importante de esta tarea)

Con una sola corrida, `price_drops()` devuelve vacío y todo lo que dependa de
historial no tiene nada que mostrar.

**No inventes historial sintético.** Un portfolio que muestra curvas de precio
fabricadas y las presenta como reales es peor que no mostrar nada. Si en algún
momento querés datos de ejemplo para desarrollo, que sea un archivo aparte,
claramente marcado como sintético, y nunca el demo por defecto.

Lo que sí hay que hacer:

- En `analyze` y en el dashboard, cuando `price_drops()` viene vacío o hay menos
  de 2 snapshots por aviso, mostrar un mensaje explícito del tipo: *"Historial de
  precios: se necesitan al menos 2 corridas. Actualmente: 1."* No un error, no
  una tabla vacía, no una sección que desaparece en silencio.
- Que ese mensaje se calcule solo. Cuando el cron lleve tres semanas, la sección
  tiene que aparecer sin tocar una línea de código.
- Mismo tratamiento para `zone_stats` cuando ningún grupo llega a
  `min_comparables`: decir *por qué* está vacío, no devolver un DataFrame vacío.

Con 150 avisos en pocas zonas es muy probable que `min_comparables: 20` deje
todo afuera. Verificalo corriendo el análisis sobre el demo; si pasa, no bajes
el umbral en `config.yaml` — el default tiene que seguir siendo estadísticamente
sano. Documentá en el README que el demo es chico y que con más datos el
análisis se enciende solo.

- Sobre los duplicados del A.1: con títulos sintéticos el filtro de similitud
  pierde sentido. Opciones, elegí una y documentala: (a) en modo demo, saltear
  la comparación de títulos y reportar solo candidatos por fingerprint,
  marcándolos como tales; (b) derivar el título sintético incluyendo algo de
  ruido que no venga del fingerprint. Preferí (a): es más honesta y más simple.

## A.5 — Regenerable

Documentá en el README el comando para regenerar el demo. Dentro de un mes, con
historial real acumulado, regenerarlo tiene que ser un comando y un commit — y
ahí el demo pasa a mostrar curvas de precio de verdad, que es lo único del
proyecto que nadie puede replicar en una tarde.

## Criterio de terminado

En una máquina limpia, sin `.env` y sin Playwright:
`git clone && pip install -r requirements.txt && python -m inmobot analyze --demo`
imprime un análisis con contenido y sin errores.

---

# TAREA B — Tests

## Objetivo

No cobertura. Señalizar que existen y atrapar las regresiones que importan.
Con cuatro fuentes y un `_map()` en cada una, un cambio en `normalize.py` puede
romper tres sin que me entere.

`pytest`. Carpeta `tests/`. Sin red, sin Playwright, sin base real: todo contra
fixtures.

## B.1 — Funciones puras (empezar acá)

`to_currency`
- USD → USD devuelve el mismo número.
- ARS → USD con `ARS_per_USD` configurado da el valor correcto.
- Sin cotización en `fx_rates` devuelve `None` (no cero, no excepción).
- `amount=None` o `currency=None` devuelve `None`.

`passes_filters`
- Aviso dentro de todos los rangos → `True`.
- Precio fuera del rango → `False` con el motivo correcto.
- **Aviso sin `covered_area` con `covered_area_min` configurado → `True`.**
  Esta es una decisión deliberada (dato ausente no descalifica). El test la fija
  para que nadie la "arregle" sin darse cuenta.
- `price_norm=None` → `False` con motivo `"sin precio normalizable"`.
- `require_photos: true` y `photo_count=0` → `False`.

`fingerprint`
- Dos avisos dentro de `area_tolerance_m2` y `price_tolerance_pct` → misma huella.
- Dos avisos claramente distintos → huellas distintas.
- Estable: misma entrada, misma salida entre corridas.

## B.2 — Análisis con datos plantados

Construí un DataFrame chico a mano (no aleatorio) donde sepas la respuesta.

`find_undervalued`
- Con un aviso plantado 30% bajo la mediana de su grupo, aparece; los demás no.
- Un grupo con menos de `min_comparables` no genera resultados.

`price_drops`
- Fixture con dos snapshots del mismo aviso a precios distintos: detecta 1 baja
  y el `total_drop_pct` correcto.
- Un solo snapshot por aviso: devuelve vacío sin romper. (Es el caso real hoy.)

`zone_stats`
- Que el recorte de outliers efectivamente excluya los extremos.

## B.3 — El test que más vale: contrato de las fuentes

Guardá en `tests/fixtures/` una respuesta JSON real de MercadoLibre (anonimizada
igual que el demo) y el HTML de una tarjeta de cada portal scrapeado. Después,
por cada fuente, un test que corra su `_map()` contra el fixture y verifique que
devuelve las claves del esquema común con los tipos correctos.

Esto es lo que atrapa el día en que MercadoLibre renombra `COVERED_AREA` o
Zonaprop cambia una clase de CSS — que es *la* forma en que estos proyectos se
rompen en producción. Es el test que un evaluador va a mirar y va a entender que
pensaste el problema.

## B.4 — Regresión del bug de `mark_inactive`

Ya está arreglado; el test evita que vuelva.
- Fixture con 100 avisos en la base. Simular una corrida donde la fuente reporta
  la zona en `incomplete_zones` y solo devuelve 3 avisos.
- Verificar que **no** se dan de baja los 97 restantes de esa zona.
- Verificar que en una zona que sí terminó completa, los que faltan **sí** se
  dan de baja.

## B.5 — Infraestructura

- `requirements-dev.txt` con `pytest`.
- Sección en el README: cómo correr los tests.
- Si sale fácil, un GitHub Action que corra `pytest` en cada push. El badge de
  verde en el README es señal barata y efectiva.

## Criterio de terminado

`pytest` pasa en limpio, sin red y sin credenciales. Entre 15 y 25 tests. Si te
pasás de 40, te fuiste de tema.

---

# Cómo trabajar esto

- Tarea A y tarea B en commits separados, y dentro de cada una, commits chicos.
  El historial del repo es parte del portfolio: hoy tiene un solo commit y eso
  se nota.
- Si algo del spec no cierra con el código real, decímelo antes de improvisar.
- No agregues features. Ni fuentes nuevas, ni geocoding, ni la regresión. El
  objetivo de esta ronda es que el proyecto sea *evaluable*, no más grande.
