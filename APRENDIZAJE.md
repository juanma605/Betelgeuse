# Qué me tiene que enseñar este proyecto

Checklist para medirme contra algo concreto en vez de "siento que estoy
aprendiendo". Cada ítem tiene una prueba: si no puedo responderla sin mirar el
código, no lo sé todavía.

Se completa en el orden de las tareas del `CLAUDE.md`, no de corrido.

---

## 1. APIs e integración

*Se aprende en la tarea 1 (hacer andar el scrape).*

- [ ] **Anatomía de un request HTTP.** Query params, headers, status codes, body.
      → *Prueba:* explicar qué hace cada línea del `client.get()` en
      `mercadolibre.py` y por qué el `User-Agent` está ahí.

- [ ] **Paginación.** Offset vs cursor, y por qué los topes existen.
      → *Prueba:* ¿por qué `max_results_per_zone` es 1000 y qué pasa si lo subo a
      5000? ¿Cómo consigo más de 1000 avisos de un barrio grande?

- [ ] **Autenticación.** Bearer tokens, expiración, por qué el token no va en el
      código.
      → *Prueba:* ¿por qué `access_token` se lee de una variable de entorno y no
      está escrito en `config.yaml`? ¿Qué pasa si commiteo un token a git?

- [ ] **Fallar bien.** Distinguir un 401 de un 429 de un timeout, y reaccionar
      distinto a cada uno.
      → *Prueba:* implementar reintentos con backoff exponencial para el 429.
      Hoy el código no lo hace — es un hueco real.

- [ ] **Rate limiting y por qué importa.** No es cortesía, es no quedarse afuera.
      → *Prueba:* ¿por qué ML tolera 0.4s entre requests y Zonaprop necesita 4s?

---

## 2. Modelado de datos y SQL

*Se aprende leyendo `db.py` y viendo crecer la base.*

- [ ] **Diseño de esquema.** Por qué estos campos, por qué dos tablas y no una.
      → *Prueba:* ¿por qué `price_snapshots` es una tabla aparte en vez de una
      columna `precio_anterior` en `listings`? (La respuesta es el corazón del
      proyecto.)

- [ ] **Clave primaria y upsert.** Idempotencia: correr dos veces no duplica.
      → *Prueba:* ¿por qué el `id` es `"mercadolibre:MLA123"` y no `MLA123` a
      secas? ¿Qué se rompe cuando agregue Zonaprop si uso solo el ID nativo?

- [ ] **Índices.** Qué aceleran, qué cuestan.
      → *Prueba:* ¿por qué hay índice en `fingerprint` y no en `title`?

- [ ] **Estado vs evento.** `listings` es un estado que se pisa; `price_snapshots`
      es un log de eventos que nunca se borra.
      → *Prueba:* nombrar otra cosa del proyecto que convendría guardar como
      evento y hoy se pierde.

- [ ] **Soft delete.** Por qué `active = 0` en vez de `DELETE`.
      → *Prueba:* ¿qué información valiosa perdería si borrara los avisos que
      desaparecen?

---

## 3. Datos sucios (acá se va el 70% del tiempo real)

*Se aprende en `normalize.py`, y es la parte que más se cobra como freelance.*

- [ ] **Esquema común sobre fuentes heterogéneas.** Cada portal nombra las cosas
      distinto; el resto del sistema no debería enterarse.
      → *Prueba:* explicar por qué `_map()` vive en la fuente y no en
      `normalize.py`.

- [ ] **Datos faltantes.** Ausente ≠ cero ≠ inválido.
      → *Prueba:* en `passes_filters`, un aviso sin `covered_area` **pasa** el
      filtro de área mínima. ¿Por qué decidí eso? ¿Cuándo sería la decisión
      equivocada?

- [ ] **Conversión de unidades y moneda.** Y por qué la cotización es un dato con
      fecha, no una constante.
      → *Prueba:* si guardo un aviso en pesos hoy y el dólar se mueve 20%,
      ¿el `price_norm` de la semana pasada sigue siendo comparable? ¿Cómo lo
      arreglo?

- [ ] **Parseo defensivo.** `_as()` existe porque los datos reales vienen rotos.
      → *Prueba:* listar tres formas en que `"55 m²"` puede llegar y romper el
      cast a float.

---

## 4. Deduplicación (entity resolution)

*Se aprende en la tarea 2, calibrando con datos reales.*

- [ ] **El problema.** No hay ID común entre portales; hay que inferir identidad.
- [ ] **Fingerprint / blocking.** Comparar todos contra todos es O(n²); agrupar
      por clave difusa primero lo hace tratable.
      → *Prueba:* con 5.000 avisos, ¿cuántas comparaciones evita el fingerprint?

- [ ] **Precisión vs recall.** El trade-off central, y que no existe el umbral
      "correcto" — existe el que conviene al costo del error.
      → *Prueba:* para *mí, comprando*, ¿qué es peor: un duplicado falso que me
      hace perder dos minutos, o un duplicado no detectado que me esconde que la
      misma unidad está 8% más barata en otra agencia? La respuesta define si
      `title_similarity` sube o baja.

- [ ] **Similitud de strings.** Qué mide `SequenceMatcher` y cuándo falla.
      → *Prueba:* dar dos títulos del mismo depto que el ratio de 0.6 no atrapa.

---

## 5. Estadística aplicada

*Se aprende en `analyze.py`, tareas 2 y 5.*

- [ ] **Mediana vs media, y por qué acá uso mediana.**
      → *Prueba:* explicar con un ejemplo de Puerto Madero en una muestra de
      Almagro.

- [ ] **Percentiles y dispersión.** p25/p75 dicen más que el promedio.
      → *Prueba:* dos barrios con la misma mediana y p75 muy distinto — ¿qué
      significa eso para el comprador?

- [ ] **Outliers.** Por qué recorto 5% antes de calcular, y qué riesgo corro.
      → *Prueba:* ¿puede el recorte estar borrando justo las oportunidades que
      busco? (Sí. Pensar por qué igual conviene.)

- [ ] **Tamaño de muestra.** Por qué `min_comparables = 20`.
      → *Prueba:* ¿qué pasa si calculo la mediana de un barrio con 3 avisos?

- [ ] **Regresión lineal múltiple.** Coeficientes, residuos, y que el residuo
      negativo *es* la señal de subvaluado.
      → *Prueba:* implementar la tarea 5 **yo**, sin Claude Code, y después
      pedirle que la critique.

- [ ] **Correlación ≠ causalidad, y overfitting.** Un modelo con 30 variables y
      200 avisos memoriza en vez de aprender.
      → *Prueba:* ¿cómo sé si mi regresión generaliza? (Buscar: train/test split.)

---

## 6. Ingeniería de software

*Transversal. Es lo que separa un script de un producto.*

- [ ] **Configuración vs código.** El principio rector del proyecto.
      → *Prueba:* encontrar algo que hoy esté hardcodeado y debería estar en el
      YAML.

- [ ] **Separación de capas.** Fuente → normalización → persistencia → análisis →
      presentación. Cada una ignora a las demás.
      → *Prueba:* ¿cuántos archivos tengo que tocar para agregar Argenprop? Si es
      más de dos, el diseño está mal.

- [ ] **Idempotencia.** Correr dos veces = correr una vez.
- [ ] **Tests.** Hoy el proyecto tiene cero. Es el hueco más grande.
      → *Prueba:* escribir un test de `to_currency()` y uno de
      `passes_filters()`. Empezar por ahí porque son funciones puras.

- [ ] **Git como red de seguridad.** Commits chicos, mensajes que explican el
      *por qué*.
      → *Prueba:* ¿puedo volver al estado de ayer sin perder nada?

---

## 7. Producto y negocio

*Lo que nadie enseña y es lo que realmente se cobra.*

- [ ] **Qué dato vale y por qué.** El aviso es commodity; el historial es activo.
      → *Prueba:* explicar en dos frases, sin tecnicismos, por qué esto vale para
      una inmobiliaria.

- [ ] **Datos que se acumulan.** Por qué el valor crece con el tiempo y es difícil
      de copiar.

- [ ] **Empaquetado.** El cliente no compra un script, compra un informe o una
      alerta.
      → *Prueba:* ¿cuál es el entregable? ¿Un CSV, un dashboard, un PDF mensual?

- [ ] **De proyecto a producto.** Detectar que tres clientes piden lo mismo.

---

## Cómo lo uso

- Reviso esto **después** de cada tarea del `CLAUDE.md`, no antes.
- Un ítem se tilda solo si respondí la prueba **sin mirar el código**.
- Lo que no puedo responder no es un fracaso: es la lista de lo que estudio
  después, y es más útil que cualquier plan armado de antemano.
- Cada dos semanas, borro un módulo chico y lo reescribo de memoria.
  `normalize.py` es el mejor candidato.
