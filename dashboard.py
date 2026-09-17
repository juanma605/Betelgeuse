"""Dashboard de mercado sobre la base de inmobot.

    streamlit run dashboard.py

Filtros + tabla ordenable + promedios por zona + destacados (avisos que se
alejan del promedio de su propio grupo zona/ambientes) + mapa si hay
lat/long. Nada de umbrales estrictos como en `analyze.py` — esto es para
explorar, no para el reporte "oficial" de subvaluados.
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
from inmobot import analyze, config, db

st.set_page_config(page_title="inmobot", layout="wide")
st.title("inmobot — mercado en vivo")

cfg = config.load("config.yaml")
with db.connect(cfg.get_path("storage.path")) as conn:
    df = analyze.load_active(conn)
    drops = analyze.price_drops(conn)

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
group_avg = filtered.groupby(["zone", "rooms"])["price_per_m2"].transform("mean")
group_n = filtered.groupby(["zone", "rooms"])["price_per_m2"].transform("size")
filtered = filtered.assign(
    avg_zona_m2=group_avg,
    vs_promedio_pct=((filtered["price_per_m2"] / group_avg) - 1) * 100,
    n_comparables=group_n,
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
cols = ["title", "zone", "rooms", "area", "price_norm", "price_per_m2", "vs_promedio_pct", "url"]
linked_cols = [c for c in cols if c != "url"]


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
    que una columna linkee usando el texto de otra)."""
    display = frame.copy()
    short_title = display["title"].str.slice(0, 55)
    display["title"] = [
        f'<a href="{u}" target="_blank" title="{t}">{s}…</a>'
        for t, s, u in zip(display["title"], short_title, display["url"])
    ]
    table_html = display[linked_cols].round(1).to_html(escape=False, index=False)
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

# --- bajaron de precio: el dato que ningún portal muestra ---------------- #
st.subheader("Bajaron de precio")
zone_drops = drops[drops["listing_id"].isin(filtered["id"])] if not drops.empty else drops
if zone_drops.empty:
    st.caption("Todavía sin bajas de precio detectadas (hace falta más de una corrida).")
else:
    with_title = zone_drops.merge(
        filtered[["id", "title", "zone", "url"]], left_on="listing_id", right_on="id"
    )
    drop_cols = ["title", "zone", "first_price", "current_price", "drops", "total_drop_pct", "url"]
    st.dataframe(with_title[drop_cols].round(1), hide_index=True, use_container_width=True)

# --- tabla completa ------------------------------------------------------#
st.subheader(f"Todos los avisos ({len(filtered)})")
st.dataframe(filtered[cols].round(1), hide_index=True, use_container_width=True)

with st.expander("Ver todas las columnas (dato crudo)"):
    st.dataframe(filtered, hide_index=True, use_container_width=True)

# --- mapa, si hay coordenadas --------------------------------------------#
with_coords = filtered.dropna(subset=["latitude", "longitude"])
if not with_coords.empty:
    st.subheader("Mapa")
    st.map(with_coords.rename(columns={"latitude": "lat", "longitude": "lon"}))
else:
    st.caption(
        "Sin coordenadas para mostrar en mapa (Zonaprop/Argenprop todavía no "
        "las capturan — pendiente)."
    )
