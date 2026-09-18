# Fixtures

Una tarjeta de resultados de cada portal, capturada del sitio real y después
**anonimizada**: la estructura (tags, clases, atributos `data-*`) es la de
producción, el contenido no. Precios, direcciones, ids y descripciones están
reemplazados por valores inventados, y se sacaron los atributos de framework
que no usa ningún selector.

Son la parte que importa: el día que Zonaprop renombre una clase o Argenprop
saque un elemento, el test de contrato se pone en rojo antes de que la base se
llene de nulls. Ya pasó una vez — ver `test_sources.py::test_argenprop_map`.

`mercadolibre_search.json` es la excepción: está **construido a mano** a partir
del esquema documentado de la API, no capturado. La búsqueda de ML devuelve
403 sin certificación de app (por eso la fuente está apagada en `config.yaml`),
así que no hay forma de sacar una respuesta real. Sirve para fijar el mapeo de
`attributes`, no para probar que ML sigue contestando lo mismo.

Para recapturar una tarjeta: abrí el listado en el navegador y copiá el
`outerHTML` del primer resultado, después limpiá el contenido.
