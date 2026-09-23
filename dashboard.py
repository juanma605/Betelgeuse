"""Dashboard de mercado sobre la base de inmobot.

    streamlit run dashboard.py
    streamlit run dashboard.py -- --demo   # contra data/demo.db, sin credenciales

Filtros + tabla ordenable + promedios por zona + destacados (avisos que se
alejan del promedio de su propio grupo zona/ambientes) + mapa si hay
lat/long. Nada de umbrales estrictos como en `analyze.py` — esto es para
explorar, no para el reporte "oficial" de subvaluados.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st
from inmobot import analyze, buscador, config, db, demo, places, yields

st.set_page_config(page_title="inmobot", layout="wide")
st.title("inmobot — mercado en vivo")

cfg = config.load("config.yaml")
# Sin `--demo`, el dashboard cae igual al demo si no hay base real: alguien
# que acaba de clonar el repo no tiene una, y una pantalla vacía no muestra
# nada del proyecto.
use_demo = "--demo" in sys.argv or not Path(cfg.get_path("storage.path")).exists()

# Solo lectura: el dashboard no escribe nada, y abrirlo en modo escritura
# lo dejaba afuera durante un scrape largo ("database is locked") por el
# `CREATE TABLE IF NOT EXISTS` que corre al conectarse.
with db.connect(demo.db_path(cfg, use_demo), readonly=True) as conn:
    df = analyze.load_active(conn)
    drops = analyze.price_drops(conn)
    history = analyze.history_note(conn)

if use_demo:
    st.info(
        "Dataset de demo: avisos reales anonimizados (sin el título ni el JSON "
        "del portal). Los links van al aviso original: si alguno está caído, "
        "es que ya se dio de baja. Para ver datos propios: `python -m inmobot scrape`."
    )

if df.empty:
    st.warning("No hay avisos activos. Corré `python -m inmobot scrape` primero.")
    st.stop()

# --- búsqueda en lenguaje natural (opcional) ---------------------------- #
# Solo aparece con `nl_search` configurado y `openai` instalado; si no, el
# dashboard es el de siempre. El modelo devuelve filtros, no SQL: ver
# inmobot/buscador.py. Recorta `df` antes que todo lo demás, así los filtros
# manuales de abajo siguen funcionando sobre el resultado.
if buscador.habilitado(cfg):
    with st.sidebar:
        pedido = st.text_input(
            "Buscar",
            placeholder="3 ambientes en Palermo o Belgrano, menos de 150 mil, más de 60 m²",
            help="Un modelo de lenguaje convierte la frase en filtros. Abajo se "
                 "muestra lo que entendió: si no es lo que querías, reescribí la frase.",
        ).strip()

    if pedido:
        # Streamlit corre el script entero con cada clic: sin esto, mover un
        # slider volvería a llamar al modelo. Los errores no se guardan, así
        # un servidor que se cayó y volvió se puede reintentar con Enter.
        guardadas = st.session_state.setdefault("busquedas_nl", {})
        with db.connect(demo.db_path(cfg, use_demo), readonly=True) as conn:
            if pedido not in guardadas:
                zonas_db = [z for (z,) in conn.execute(
                    "SELECT DISTINCT zone FROM listings WHERE active = 1 AND zone IS NOT NULL"
                )]
                with st.spinner("Interpretando la búsqueda..."):
                    interpretado = buscador.interpretar(pedido, zonas_db, cfg)
                if interpretado["error"] is None:
                    guardadas[pedido] = interpretado
            else:
                interpretado = guardadas[pedido]

            columnas = {fila["name"] for fila in conn.execute("PRAGMA table_info(listings)")}
            filtros_nl, sin_columna = buscador.quitar_sin_columna(interpretado["filtros"], columnas)
            if filtros_nl:
                sql, params = buscador.construir_sql(filtros_nl)
                ids_nl = {fila["id"] for fila in conn.execute(sql, params)}

        with st.sidebar:
            if interpretado["error"]:
                st.warning(interpretado["error"] + " Sigo con los filtros manuales.")
            if filtros_nl:
                st.caption("Entendí: " + buscador.describir(
                    filtros_nl, cfg.get_path("search.currency", "USD")
                ))
            for aviso in interpretado["avisos"] + sin_columna:
                st.caption("⚠ " + aviso)

        if filtros_nl:
            df = df[df["id"].isin(ids_nl)]
            if df.empty:
                st.info(
                    "Ningún aviso activo cumple la búsqueda "
                    f"({buscador.describir(filtros_nl, cfg.get_path('search.currency', 'USD'))}). "
                    "Probá aflojar algún filtro o borrá la frase para ver todo."
                )
                st.stop()

# Distancia a lo que hace mejor o peor a una ubicación. Se calcula sobre la
# base entera y antes de filtrar, porque ahora también se filtra por esto.
_distancias = places.distancias(df)
df = df.assign(
    subte_m=_distancias["subte_m"].round(0),
    evitar_m=_distancias["evitar_m"].round(0),
)
SIN_TOPE = 3000          # el slider al máximo significa "no filtres"
sin_ubicacion = int(df["subte_m"].isna().sum())

# --- filtros ---------------------------------------------------------- #
with st.sidebar:
    st.header("Filtros")
    zones = st.multiselect("Zona", sorted(df["zone"].dropna().unique()), default=None)
    rooms = st.multiselect(
        "Ambientes", sorted(int(r) for r in df["rooms"].dropna().unique()), default=None,
        help="Los avisos que no publican la cantidad de ambientes quedan afuera al filtrar.",
    )
    sources = st.multiselect("Fuente", sorted(df["source"].unique()), default=None)
    incl_off_plan = st.checkbox("Incluir pozo/emprendimientos", value=False)
    pmin, pmax = int(df["price_norm"].min()), int(df["price_norm"].max())
    # Con un solo precio (una búsqueda que deja un aviso) st.slider falla si
    # el mínimo y el máximo son iguales.
    pmax = max(pmax, pmin + 1)
    price_range = st.slider("Precio", pmin, pmax, (pmin, pmax))

    st.subheader("Ubicación")
    max_subte = st.slider(
        "Máxima distancia al subte (m)", 200, SIN_TOPE, SIN_TOPE, step=100,
        help="Al tope no filtra nada. 1.000 m son unos 12 minutos caminando.",
    )
    min_evitar = st.slider(
        "Mínima distancia a hospital o cuartel (m)", 0, 1000, 0, step=50,
        help="Descarta los que están MÁS CERCA que esto. En 0 no filtra nada. "
             "Son las dos fuentes de sirenas a cualquier hora.",
    )
    solo_ubicados = st.checkbox(
        "Solo avisos con ubicación conocida", value=False,
        help=f"{sin_ubicacion} avisos no publican dirección ni coordenadas. "
             "Sin esto, siguen apareciendo aunque filtres por distancia: no "
             "sabemos dónde están, no que estén mal ubicados.",
    )

filtered = df.copy()
if zones:
    filtered = filtered[filtered["zone"].isin(zones)]
if rooms:
    filtered = filtered[filtered["rooms"].isin(rooms)]
if sources:
    filtered = filtered[filtered["source"].isin(sources)]
if not incl_off_plan:
    filtered = filtered[~filtered["off_plan"]]
filtered = filtered[filtered["price_norm"].between(*price_range)]

# Un aviso sin coordenadas no se descarta por distancia: no sabemos dónde
# está, que es distinto de saber que está lejos. Para sacarlos está el
# checkbox, que es una decisión aparte y explícita.
if solo_ubicados:
    filtered = filtered[filtered["subte_m"].notna()]
if max_subte < SIN_TOPE:
    filtered = filtered[filtered["subte_m"].isna() | (filtered["subte_m"] <= max_subte)]
if min_evitar > 0:
    filtered = filtered[filtered["evitar_m"].isna() | (filtered["evitar_m"] >= min_evitar)]

# --- promedio del propio grupo (zona + ambientes), sin piso de muestra -- #
# El promedio de referencia sale solo de avisos con m² cubiertos, igual que
# las medianas de analyze.py: los de m² totales se comparan contra él pero no
# lo definen, porque lo tirarían para abajo.
covered_m2 = filtered["price_per_m2"].where(filtered["area_source"] == "covered")
by_group = covered_m2.groupby([filtered["zone"], filtered["rooms"]])
group_avg = by_group.transform("mean")
filtered = filtered.assign(
    avg_zona_m2=group_avg,
    vs_promedio_pct=((filtered["price_per_m2"] / group_avg) - 1) * 100,
    n_comparables=by_group.transform("count"),
    area_estimada=filtered["area_source"] == "total",
)

evitar_m = cfg.get_path("analysis.location.evitar_m", 200)

# --- lo que rinde el edificio, si hay alquileres del mismo punto --------- #
# El alquiler no sale del aviso —un aviso de venta no dice cuánto se alquila—
# sino de la base de alquileres, cruzando por edificio: dos avisos son del
# mismo cuando su dirección geocodifica al mismo punto. Ver inmobot/yields.py.
#
# Lo que se muestra no es "el alquiler de ese departamento" (no existe) sino
# lo que rendiría ESE metraje a los USD/m² que se alquila el edificio. Por
# eso un 3 ambientes y un monoambiente de la misma torre dan números
# distintos aunque salgan del mismo dato.
filtered["alquiler_mes"] = pd.NA
filtered["rinde_anual_pct"] = pd.NA
filtered["alquiler_url"] = pd.NA

_ruta_alquileres = Path(cfg.get_path("storage.rentals_path", "data/rentals.db"))
if not use_demo and _ruta_alquileres.exists():
    with db.connect(_ruta_alquileres, readonly=True) as _conn_alq:
        _por_edificio = yields._lado(_conn_alq, "alquiler")
    if not _por_edificio.empty:
        _punto = list(zip(filtered["latitude"].round(6), filtered["longitude"].round(6)))
        # Solo los avisos que publican dirección tienen un punto del
        # geocodificador; los que traen coordenadas del portal son
        # aproximadas y no identifican un edificio.
        _con_direccion = filtered["address"].notna() & filtered["address"].str.strip().ne("")
        _alq_m2 = pd.Series(_punto, index=filtered.index).map(
            _por_edificio["alquiler_por_m2"]
        ).where(_con_direccion)
        filtered["alquiler_mes"] = (_alq_m2 * filtered["area"]).round(0)
        filtered["rinde_anual_pct"] = (
            100 * _alq_m2 * 12 / filtered["price_per_m2"]
        ).round(1)
        # Un alquiler cualquiera de ese edificio, para abrirlo y comprobar
        # que la dirección es la misma. Si el cruce se equivoca, se ve acá.
        filtered["alquiler_url"] = pd.Series(_punto, index=filtered.index).map(
            _por_edificio["alquiler_url"]
        ).where(_con_direccion)

REFERENCIAS = {
    "subte": ("Subte", [40, 120, 220]),
    "hospitales": ("Hospital con guardia", [235, 150, 35]),
    "bomberos": ("Cuartel de bomberos", [150, 85, 200]),
}

AREA_NOTE = (
    "m² totales, no cubiertos: el aviso no publica la superficie cubierta "
    "(Zonaprop no la muestra en el listado). Su precio/m² sale más bajo "
    "de lo real, así que si aparece como barato puede ser un PH con patio o un "
    "balcón grande, no una oportunidad."
)

# --- KPIs --------------------------------------------------------------- #
c1, c2, c3, c4 = st.columns(4)
c1.metric("Avisos", len(filtered))
c2.metric("Precio promedio", f"USD {filtered['price_norm'].mean():,.0f}")
c3.metric("USD/m² promedio", f"{filtered['price_per_m2'].mean():,.0f}")
c4.metric("Fuentes", filtered["source"].nunique())

st.caption("Avisos por fuente: " + " · ".join(
    f"{src} ({n})" for src, n in filtered["source"].value_counts().items()
))

# --- promedio por zona ---------------------------------------------------#
st.subheader("USD/m² promedio por zona")
st.bar_chart(filtered.groupby("zone")["price_per_m2"].mean())

# --- destacados: se alejan del promedio de su propio grupo -------------- #
st.subheader("Destacados (vs. el promedio de su zona/ambientes)")
comparable = filtered[filtered["n_comparables"] >= 2]
cols = [
    "title", "zone", "rooms", "area", "area_estimada", "price_norm",
    "price_per_m2", "vs_promedio_pct", "alquiler_mes", "rinde_anual_pct",
    "alquiler_url", "subte_m", "url",
]
# `url` y `alquiler_url` no se linkean sobre sí mismas: la celda ya ES el
# link. `area_estimada` es un booleano, no tiene adónde llevar.
linked_cols = [c for c in cols if c not in ("url", "alquiler_url", "area_estimada")]


_TABLE_CSS = """
<style>
.inmobot-table { width: 100%; overflow-x: auto; font-size: 0.85rem; }
.inmobot-table table { width: 100%; border-collapse: collapse; }
.inmobot-table th, .inmobot-table td {
    padding: 6px 10px; text-align: left; white-space: nowrap;
    border-bottom: 1px solid rgba(128, 128, 128, 0.3);
}
.inmobot-table td:first-child { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.inmobot-table a { color: inherit; text-decoration: underline; }
</style>
"""


def sortable_table(frame: pd.DataFrame, columns: list[str], key: str, default_col: str) -> None:
    """Tabla que ordena Python y no el navegador, con los vacíos siempre al fondo.

    El orden nativo de st.dataframe (clic en el encabezado) lo hace la grilla
    del navegador invirtiendo la comparación para "mayor a menor": los vacíos
    que quedan abajo en un sentido suben arriba en el otro, y no se puede
    configurar. Acá el clic en un encabezado *selecciona* la columna (con la
    selección activa, Streamlit apaga el orden nativo) y el orden lo hace
    pandas con na_position="last", que deja los vacíos al final en los dos
    sentidos. Solo importan los vacíos de la columna elegida.
    """
    picked = ((st.session_state.get(key) or {}).get("selection") or {}).get("columns") or []
    sort_col = picked[0] if picked and picked[0] in frame.columns else default_col
    direction = st.radio(
        "Orden", ["Mayor a menor", "Menor a mayor"], horizontal=True,
        key=f"{key}_orden", label_visibility="collapsed",
    )
    st.caption(
        f"Ordenado por **{sort_col}**, {direction.lower()}. Clic en el nombre de una "
        "columna para ordenar por esa; los datos vacíos quedan siempre al final."
    )
    shown = frame.sort_values(sort_col, ascending=direction == "Menor a mayor", na_position="last")
    st.dataframe(
        shown[columns].round(1), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-column", key=key,
        column_config={
            "url": st.column_config.LinkColumn("url"),
            "alquiler_url": st.column_config.LinkColumn(
                "alquiler_url",
                help="Un alquiler de ese mismo edificio. Abrilo para "
                     "comprobar que la dirección coincide con la del aviso.",
            ),
        },
    )


def _table_with_link(frame: "pd.DataFrame") -> None:
    """Tabla HTML con el título como link al aviso (st.dataframe no permite
    que una columna linkee usando el texto de otra). Sin URL queda el texto
    solo."""
    display = frame.copy()
    short_title = display["title"].str.slice(0, 55)
    display["title"] = [
        f'<a href="{u}" target="_blank" title="{t}">{s}…</a>' if u else s
        for t, s, u in zip(display["title"], short_title, display["url"])
    ]
    display = display.round(1)
    display["area"] = [
        f"{a:g}*" if est else f"{a:g}" for a, est in zip(display["area"], display["area_estimada"])
    ]
    table_html = display[linked_cols].to_html(escape=False, index=False)
    st.markdown(_TABLE_CSS + f'<div class="inmobot-table">{table_html}</div>', unsafe_allow_html=True)


col_a, col_b = st.columns(2)
with col_a:
    st.caption("Más baratos que el promedio de su grupo")
    cheap = comparable.sort_values("vs_promedio_pct").head(10)
    _table_with_link(cheap)
with col_b:
    st.caption("Más caros que el promedio de su grupo")
    expensive = comparable.sort_values("vs_promedio_pct", ascending=False).head(10)
    _table_with_link(expensive)
if cheap["area_estimada"].any() or expensive["area_estimada"].any():
    st.caption("\\* " + AREA_NOTE)

# --- bajaron de precio: el dato que ningún portal muestra ---------------- #
st.subheader("Bajaron de precio")
zone_drops = drops[drops["listing_id"].isin(filtered["id"])] if not drops.empty else drops
if zone_drops.empty:
    st.caption(history or "Ningún aviso de los filtrados bajó de precio todavía.")
else:
    with_title = zone_drops.merge(
        filtered[["id", "title", "zone", "url"]], left_on="listing_id", right_on="id"
    )
    drop_cols = ["title", "zone", "first_price", "current_price", "drops", "total_drop_pct", "url"]
    sortable_table(with_title, drop_cols, key="tabla_bajas", default_col="total_drop_pct")

# --- tabla completa ------------------------------------------------------#
st.subheader(f"Todos los avisos ({len(filtered)})")
if filtered["alquiler_mes"].notna().any():
    st.warning(
        "**`alquiler_mes` y `rinde_anual_pct` no son confiables todavía.** Salen "
        "de escalar por m² el alquiler del edificio, y medido contra los "
        "alquileres reales, un 36% cae muy fuera de lo que ese edificio alquila "
        "de verdad: un edificio que solo publica monoambientes no dice nada "
        "sobre un departamento de 200 m². Abrí `alquiler_url` y comparalo antes "
        "de usar el número."
    )
sortable_table(filtered, cols, key="tabla_todos", default_col="price_per_m2")
if filtered["area_estimada"].any():
    st.caption(
        f"{int(filtered['area_estimada'].sum())} de {len(filtered)} con `area_estimada`: "
        + AREA_NOTE
    )

with st.expander("Ver todas las columnas (dato crudo)"):
    sortable_table(filtered, list(filtered.columns), key="tabla_crudo", default_col="price_per_m2")

# --- mapa: los mismos avisos que la tabla, los que tienen ubicación ------#
st.subheader("Mapa")
located = filtered.dropna(subset=["latitude", "longitude"])
unlocated = len(filtered) - len(located)

if located.empty:
    st.info(
        f"Ninguno de los {len(filtered)} avisos filtrados tiene ubicación. Hoy solo "
        "Remax y Mudafy la publican en el listado."
    )
else:
    col_a, col_b, col_c = st.columns(3)
    mostrar = {
        "subte": col_a.checkbox("Subtes", value=True),
        "hospitales": col_b.checkbox("Hospitales con guardia", value=True),
        "bomberos": col_c.checkbox("Bomberos", value=True),
    }

    def _m2(area, estimada):
        return f"{area:.0f} m² totales* (no publica cubiertos)" if estimada else f"{area:.0f} m²"

    points = pd.DataFrame({
        "latitude": located["latitude"],
        "longitude": located["longitude"],
        "zone": located["zone"],
        "rooms": [f"{r:.0f} amb" if pd.notna(r) else "amb. s/d" for r in located["rooms"]],
        "m2": [_m2(a, e) for a, e in zip(located["area"], located["area_estimada"])],
        "price": [f"USD {p:,.0f}" for p in located["price_norm"]],
        "price_m2": [f"USD {p:,.0f}/m²" for p in located["price_per_m2"]],
        "title": located["title"],
        "url": located["url"],
    })
    points["tooltip"] = [
        f"<b>{z}</b> · {r}<br/>{m}<br/>{p} · {pm}<br/>Subte a {s:,.0f} m"
        f"{f'<br/>⚠ hospital o bomberos a {e:,.0f} m' if pd.notna(e) and e < evitar_m else ''}"
        "<br/><i>clic para ver el link</i>"
        for z, r, m, p, pm, s, e in zip(
            points["zone"], points["rooms"], points["m2"], points["price"],
            points["price_m2"], located["subte_m"], located["evitar_m"],
        )
    ]

    # compute_view da un zoom que abarca todos los puntos, pero centra en el
    # promedio: un cúmulo denso lo arrastra y deja afuera los de los bordes.
    # Se centra en el medio del recuadro. Con un solo punto se iría a zoom 21.
    view = pdk.data_utils.compute_view(points[["longitude", "latitude"]].values.tolist())
    view.latitude = (points["latitude"].min() + points["latitude"].max()) / 2
    view.longitude = (points["longitude"].min() + points["longitude"].max()) / 2
    view.zoom = min(view.zoom, 15)

    capas = [pdk.Layer(
        "ScatterplotLayer",
        id="avisos",
        data=points,
        get_position=["longitude", "latitude"],
        get_radius=35,
        radius_min_pixels=4,
        get_fill_color=[220, 70, 50, 190],
        pickable=True,
        auto_highlight=True,
    )]
    for clave, (etiqueta, color) in REFERENCIAS.items():
        if not mostrar[clave]:
            continue
        lugares = pd.DataFrame(places.cargar()[clave])
        lugares["tooltip"] = [f"<b>{etiqueta}</b><br/>{n}" for n in lugares["nombre"]]
        capas.append(pdk.Layer(
            "ScatterplotLayer",
            id=clave,
            data=lugares,
            get_position=["lon", "lat"],
            get_radius=30,
            radius_min_pixels=5,
            get_fill_color=color + [210],
            pickable=True,
        ))

    deck = pdk.Deck(
        layers=capas,
        initial_view_state=view,
        # Un solo campo "tooltip" ya armado por capa: si el HTML nombrara
        # columnas (zone, rooms...), las capas de subte u hospitales las
        # mostrarían vacías.
        tooltip={"html": "{tooltip}"},
    )
    event = st.pydeck_chart(deck, on_select="rerun", selection_mode="single-object", key="mapa")

    # El tooltip desaparece cuando el mouse se va, así que el link no puede
    # vivir ahí: el clic selecciona el punto y el aviso aparece abajo.
    picked = ((event.selection or {}).get("objects") or {}).get("avisos") or []
    if picked:
        p = picked[0]
        st.markdown(f"**{p['zone']} · {p['rooms']} · {p['m2']} · {p['price']}** — "
                    + (f"[ver aviso]({p['url']})" if p.get("url") else "sin link"))

    def _punto(color, texto):
        return (f'<span style="color:rgb({color[0]},{color[1]},{color[2]})">●</span> '
                f'<span style="font-size:0.8rem">{texto}</span>')

    st.markdown(
        " &nbsp; ".join(
            [_punto([220, 70, 50], f"avisos ({len(points)})")]
            + [_punto(color, etiqueta) for clave, (etiqueta, color) in REFERENCIAS.items()
               if mostrar[clave]]
        ),
        unsafe_allow_html=True,
    )
    st.caption(
        "Ubicaciones aproximadas (~100 m): Mudafy las publica redondeadas"
        + (" y el demo redondea todas." if use_demo else ".")
        + " Subtes y bomberos: Datos Abiertos GCBA (CC-BY 2.5 AR). Hospitales con"
        " guardia: OpenStreetMap (ODbL)."
    )
    if unlocated:
        st.caption(
            f"{unlocated} de {len(filtered)} avisos sin ubicación, no se muestran en el "
            "mapa (solo Remax y Mudafy la publican en el listado)."
        )
