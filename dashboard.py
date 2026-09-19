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
from inmobot import analyze, config, db, demo

st.set_page_config(page_title="inmobot", layout="wide")
st.title("inmobot — mercado en vivo")

cfg = config.load("config.yaml")
# Sin `--demo`, el dashboard cae igual al demo si no hay base real: alguien
# que acaba de clonar el repo no tiene una, y una pantalla vacía no muestra
# nada del proyecto.
use_demo = "--demo" in sys.argv or not Path(cfg.get_path("storage.path")).exists()

with db.connect(demo.db_path(cfg, use_demo)) as conn:
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

# --- filtros ---------------------------------------------------------- #
with st.sidebar:
    st.header("Filtros")
    zones = st.multiselect("Zona", sorted(df["zone"].dropna().unique()), default=None)
    sources = st.multiselect("Fuente", sorted(df["source"].unique()), default=None)
    incl_off_plan = st.checkbox("Incluir pozo/emprendimientos", value=False)
    pmin, pmax = int(df["price_norm"].min()), int(df["price_norm"].max())
    price_range = st.slider("Precio", pmin, pmax, (pmin, pmax))

filtered = df.copy()
if zones:
    filtered = filtered[filtered["zone"].isin(zones)]
if sources:
    filtered = filtered[filtered["source"].isin(sources)]
if not incl_off_plan:
    filtered = filtered[~filtered["off_plan"]]
filtered = filtered[filtered["price_norm"].between(*price_range)]

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

AREA_NOTE = (
    "m² totales, no cubiertos: el aviso no publica la superficie cubierta "
    "(Zonaprop y Mudafy no la muestran en el listado). Su precio/m² sale más bajo "
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
    "price_per_m2", "vs_promedio_pct", "url",
]
linked_cols = [c for c in cols if c not in ("url", "area_estimada")]


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
    st.dataframe(with_title[drop_cols].round(1), hide_index=True, width="stretch")

# --- tabla completa ------------------------------------------------------#
st.subheader(f"Todos los avisos ({len(filtered)})")
st.dataframe(filtered[cols].round(1), hide_index=True, width="stretch")
if filtered["area_estimada"].any():
    st.caption(
        f"{int(filtered['area_estimada'].sum())} de {len(filtered)} con `area_estimada`: "
        + AREA_NOTE
    )

with st.expander("Ver todas las columnas (dato crudo)"):
    st.dataframe(filtered, hide_index=True, width="stretch")

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

    # compute_view da un zoom que abarca todos los puntos, pero centra en el
    # promedio: un cúmulo denso lo arrastra y deja afuera los de los bordes.
    # Se centra en el medio del recuadro. Con un solo punto se iría a zoom 21.
    view = pdk.data_utils.compute_view(points[["longitude", "latitude"]].values.tolist())
    view.latitude = (points["latitude"].min() + points["latitude"].max()) / 2
    view.longitude = (points["longitude"].min() + points["longitude"].max()) / 2
    view.zoom = min(view.zoom, 15)

    deck = pdk.Deck(
        layers=[pdk.Layer(
            "ScatterplotLayer",
            id="avisos",
            data=points,
            get_position=["longitude", "latitude"],
            get_radius=35,
            radius_min_pixels=4,
            get_fill_color=[220, 70, 50, 190],
            pickable=True,
            auto_highlight=True,
        )],
        initial_view_state=view,
        tooltip={"html": "<b>{zone}</b> · {rooms}<br/>{m2}<br/>{price} · {price_m2}"
                         "<br/><i>clic para ver el link</i>"},
    )
    event = st.pydeck_chart(deck, on_select="rerun", selection_mode="single-object", key="mapa")

    # El tooltip desaparece cuando el mouse se va, así que el link no puede
    # vivir ahí: el clic selecciona el punto y el aviso aparece abajo.
    picked = ((event.selection or {}).get("objects") or {}).get("avisos") or []
    if picked:
        p = picked[0]
        st.markdown(f"**{p['zone']} · {p['rooms']} · {p['m2']} · {p['price']}** — "
                    + (f"[ver aviso]({p['url']})" if p.get("url") else "sin link"))

    st.caption(
        "Ubicaciones aproximadas (~100 m): Mudafy las publica redondeadas"
        + (" y el demo redondea todas." if use_demo else ".")
    )
    if unlocated:
        st.caption(
            f"{unlocated} de {len(filtered)} avisos sin ubicación, no se muestran en el "
            "mapa (solo Remax y Mudafy la publican en el listado)."
        )
