"""Fuente: Remax.

Sin API pública: Playwright igual que las demás. A diferencia de
Zonaprop/Argenprop/Mudafy, acá el robots.txt es muy permisivo (solo
desautoriza a AhrefsBot entero y `?associate` para el resto) y sí soporta
paginación real vía `?page=N` (0-indexado) sobre la URL de búsqueda —
verificado navegando manualmente: `?page=1` trae resultados totalmente
distintos a la página base. No hay un tope "duro" impuesto por el sitio
como en las otras fuentes; igual no conviene pasarse de unas pocas páginas
por corrida.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Iterator

from ..normalize import slug
from ._browser import browser_page, is_bot_challenge

log = logging.getLogger(__name__)

BASE = "https://www.remax.com.ar"
MAX_PAGES = 3

CARD_SELECTOR = ".card-remax"
# El transfer state llega con el HTML inicial, antes de que Angular
# dibuje una sola tarjeta: esperar por él en vez de por `.card-remax`
# baja la página de 7,8 s a 2,0 s. Y como el `pageSize` de la URL se
# reenvía a la API, en esos 2,6 s entran 100 avisos en vez de 24.
STATE_SELECTOR = "#ng-state"

PAGINAS_POR_AVISO = 10

# Angular deja los resultados de la búsqueda serializados en este <script>
# (transfer state) para no volver a pedirlos en el cliente. Ahí viene la
# ubicación de cada aviso, que la tarjeta no muestra.
STATE_JS = "() => document.getElementById('ng-state')?.textContent || ''"


class RemaxSource:
    name = "remax"

    def __init__(self, conf: dict):
        self.property_slug = conf.get("property_slug", "departamentos")
        self.operation_slug = conf.get("operation_slug", "venta")
        self.delay = float(conf.get("rate_limit_seconds", 4.0))
        self.max_pages = int(conf.get("max_pages", MAX_PAGES))
        # Remax desambigua los barrios repetidos con la provincia pegada al
        # slug. Ver _url().
        self.zone_suffix = conf.get("zone_suffix", "")
        self.page_size = int(conf.get("page_size", 100))
        self.incomplete_zones: set[str] = set()

    def _url(self, zone: str, page: int) -> str:
        """URL de búsqueda de una zona.

        El sufijo no es cosmético: sin él, `-en-palermo` es un landing
        residual de 1 aviso (el barrio de verdad, con 1466, es
        `-en-palermo-capital-federal`) y `-en-villa-urquiza` devuelve 0.
        Remax no responde 404 ante un slug que no reconoce: devuelve otra
        búsqueda, más chica o más grande, sin avisar. Por eso Palermo
        estuvo aportando un solo aviso sin que nada se pusiera en rojo.
        """
        zona = slug(zone) + self.zone_suffix
        base = f"{BASE}/{self.property_slug}-en-{self.operation_slug}-en-{zona}"
        # Remax pagina con ?page=N pero 0-indexado: nuestra página 2 es su
        # "?page=1". El pageSize lo reenvía tal cual a su API.
        return f"{base}?page={page - 1}&pageSize={self.page_size}"

    # ---------------------------------------------------------------- #

    def fetch(self, zone: str, search_cfg: dict) -> Iterator[dict]:
        traidos = 0
        with browser_page() as page_obj:
            for page_num in range(1, self.max_pages + 1):
                url = self._url(zone, page_num)
                try:
                    page_obj.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page_obj.wait_for_selector(
                        STATE_SELECTOR, timeout=20000, state="attached"
                    )
                    state = page_obj.evaluate(STATE_JS)
                except Exception as exc:
                    self.incomplete_zones.add(zone)
                    if is_bot_challenge(page_obj):
                        log.warning(
                            "[remax] verificación anti-bot en %s (pág %d) — corto acá, "
                            "no la esquivamos. Quedaron %d página(s).",
                            zone, page_num, page_num - 1,
                        )
                    else:
                        log.info("[remax] corto en %s (pág %d): %s", zone, page_num, exc)
                    return

                avisos = listings_from_state(state)
                if not avisos:
                    # Pasarse de la última página devuelve la lista vacía. Es
                    # el final, no una falla: la zona no se marca incompleta,
                    # así sus avisos vendidos sí se dan de baja.
                    log.info(
                        "[remax] %s: se acabaron los resultados en la pág %d.",
                        zone, page_num,
                    )
                    return

                if busqueda_degradada(geo_labels(state), zone):
                    self.incomplete_zones.add(zone)
                    log.warning(
                        "[remax] %s no es un barrio que Remax reconozca: devolvió "
                        "otra búsqueda (%s). Corto la zona sin guardar nada.",
                        zone, ", ".join(sorted(set(geo_labels(state)))[:3]) or "sin etiquetas",
                    )
                    return

                if page_num % PAGINAS_POR_AVISO == 0:
                    log.info(
                        "[remax] %s: %d páginas, %d avisos y sigo...",
                        zone, page_num, traidos,
                    )

                for aviso in avisos:
                    item = self._map(aviso, zone)
                    if item:
                        traidos += 1
                        yield item

                if page_num < self.max_pages:
                    time.sleep(self.delay)

    # ---------------------------------------------------------------- #

    def _map(self, aviso: dict, zone: str) -> dict | None:
        """Un aviso del transfer state al esquema común.

        Sale del mismo JSON que alimenta las tarjetas, así que trae lo que
        la tarjeta muestra y algo más: el barrio (`geoLabel`, que el HTML no
        expone) y las dos superficies como números, sin el "33.420" ambiguo
        que obligaba a adivinar si el punto era decimal o de miles.
        """
        source_id = aviso.get("slug")
        if not source_id or aviso.get("entrepreneurship"):
            # Un emprendimiento publica el precio de la unidad más chica
            # contra el rango de superficies. Mismo criterio que en
            # zonaprop y mercadolibre.
            return None

        barrio, ciudad = _parse_geo_label(aviso.get("geoLabel"))
        item: dict = {
            "id": f"{self.name}:{source_id}",
            "source": self.name,
            "source_id": source_id,
            "url": f"{BASE}/listings/{source_id}",
            "title": (aviso.get("title") or "").strip() or zone,
            "zone": zone,
            "price": _numero(aviso.get("price")),
            "currency": _valor(aviso.get("currency")),
            "neighborhood": barrio,
            "city": ciudad,
            "covered_area": _numero(aviso.get("dimensionCovered")),
            "total_area": _numero(aviso.get("dimensionTotalBuilt")),
            "rooms": _numero(aviso.get("totalRooms")),
            "bedrooms": _numero(aviso.get("bedrooms")),
            "bathrooms": _numero(aviso.get("bathrooms")),
            "photo_count": len(aviso.get("photos") or []),
        }
        if _valor(aviso.get("expensesCurrency")):
            item["maintenance_fee"] = _numero(aviso.get("expensesPrice"))

        par = (aviso.get("location") or {}).get("coordinates")
        if isinstance(par, list) and len(par) == 2:
            lon, lat = par                      # GeoJSON: [lon, lat]
            item["latitude"], item["longitude"] = float(lat), float(lon)
        return item


def _numero(valor) -> float | None:
    """0 y None son lo mismo acá: Remax rellena con 0 lo que no publica."""
    if isinstance(valor, (int, float)) and valor:
        return float(valor)
    return None


def _valor(campo) -> str | None:
    return campo.get("value") if isinstance(campo, dict) else None


def _parse_geo_label(label: str | None) -> tuple[str | None, str | None]:
    """"Palermo, Capital Federal" -> ("Palermo", "Capital Federal")."""
    if not label:
        return None, None
    partes = [p.strip() for p in label.split(",")]
    return partes[0] or None, (partes[1] if len(partes) > 1 else None)


def geo_labels(state_json: str | None) -> list[str]:
    """Los `geoLabel` ("Palermo, Capital Federal") que trae el transfer state.

    Contestan la única pregunta que Remax no contesta sola: ¿esto es la
    búsqueda que pedí? Ver `busqueda_degradada`.
    """
    return [
        a["geoLabel"] for a in listings_from_state(state_json)
        if isinstance(a.get("geoLabel"), str)
    ]


def busqueda_degradada(labels: list[str], zone: str) -> bool:
    """True si los avisos que volvieron no son de la zona que pedimos.

    Remax no responde 404 ante un slug de zona que no reconoce: devuelve
    otra búsqueda. `-en-palermo` daba 1 aviso, `-en-canitas-capital-federal`
    los 22.864 del país entero, y `-en-belgrano-r-capital-federal` contestó
    con Beccar y Belén de Escobar. Sin esto el scraper no tiene forma de
    notarlo: los avisos son válidos, tienen precio, m² y fotos — están en
    otra ciudad.

    Alcanza con que el barrio pedido aparezca en alguno de los geoLabel,
    porque Remax mete vecinos en los resultados; lo que delata a una
    búsqueda degradada es que no aparezca en ninguno.
    """
    if not labels:
        return False  # sin dato no acusamos: puede ser un cambio del state
    pedido = slug(zone)
    return not any(pedido in slug(label) for label in labels)


def listings_from_state(state_json: str | None) -> list[dict]:
    """Los avisos que el transfer state trae de la API de búsqueda.

    La clave de primer nivel es un hash que Angular cambia entre builds, así
    que se busca por la URL de la request en vez de por una ruta fija.
    """
    try:
        estado = json.loads(state_json or "")
    except ValueError:
        return []
    if not isinstance(estado, dict):
        return []
    for valor in estado.values():
        if not isinstance(valor, dict):
            continue
        if "findAllWithEntrepreneurships" not in str(valor.get("u", "")):
            continue
        datos = (valor.get("b") or {}).get("data") or {}
        avisos = datos.get("data")
        if isinstance(avisos, list):
            return [a for a in avisos if isinstance(a, dict)]
    return []




def build(conf: dict) -> RemaxSource:
    return RemaxSource(conf)
