# Fixtures

Una tarjeta de resultados de cada portal, capturada del sitio real y después
**anonimizada**: la estructura (tags, clases, atributos `data-*`) es la de
producción, el contenido no. Precios, direcciones, ids y descripciones están
reemplazados por valores inventados, y se sacaron los atributos de framework
que no usa ningún selector.

Son la parte que importa: el día que Zonaprop renombre una clase o Argenprop
saque un elemento, el test de contrato se pone en rojo antes de que la base se
llene de nulls. Ya pasó una vez — ver `test_sources.py::test_argenprop_map`.

`mercadolibre_page.html` trae varias tarjetas en vez de una, porque lo que se
prueba ahí es qué se descarta: publicidad, emprendimientos, y un área sin
calificar que no se puede tomar como cubierta. `remax_state.json` y
`mudafy_payload.html` son los datos que cada sitio serializa en la página
(coordenadas incluidas), no tarjetas.

Para recapturar una tarjeta: abrí el listado en el navegador y copiá el
`outerHTML` del primer resultado, después limpiá el contenido.
