"""Análisis.

Acá está el valor real del proyecto: los portales te muestran avisos, esto
te muestra el mercado. Todos los umbrales salen del config.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

import pandas as pd

# Preventa/pozo: precio de m² estructuralmente más bajo que la reventa
# (se paga en cuotas, se entrega a futuro). Mezclarlo en la misma mediana de
# zona/ambientes infla artificialmente el "descuento" de find_undervalued —
# calibrado contra títulos reales, ver CLAUDE.md tarea 2.
#
# "desarrollo" y "proyecta" atrapan avisos de pozo que no dicen "pozo" ni
# "emprendimiento" explícitamente (ej. "nuevo desarrollo boutique", "se
# proyectan para ser habitados"). "reciclaje completo" es la frase completa
# a propósito, no solo "reciclaje": un depto individual reciclado/refaccionado
# es reventa normal, "reciclaje completo" en cambio describe un edificio
# entero vendido como proyecto — perderíamos comparables válidos si
# excluyéramos cualquier reventa que mencione una reforma pasada.
_OFF_PLAN_PATTERN = re.compile(
    r"emprendimiento|xintel|en construcci|a estrenar|pozo|proyecto\b"
    r"|desarrollo|proyecta|reciclaje completo",
    re.IGNORECASE,
)


def load_active(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM listings WHERE active = 1", conn)
    if df.empty:
        return df
    df["area"] = df["covered_area"].fillna(df["total_area"])
    df = df[(df["area"] > 0) & (df["price_norm"] > 0)]
    df["price_per_m2"] = df["price_norm"] / df["area"]
    df["off_plan"] = df["title"].fillna("").str.contains(_OFF_PLAN_PATTERN)
    return df


def _trim_outliers(series: pd.Series, trim_pct: float) -> pd.Series:
    if trim_pct <= 0 or series.empty:
        return series
    low = series.quantile(trim_pct / 100)
    high = series.quantile(1 - trim_pct / 100)
    return series[(series >= low) & (series <= high)]


def zone_stats(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Mediana y percentiles de precio/m² por zona y cantidad de ambientes.

    Excluye off_plan (pozo/emprendimientos): su precio/m² no es comparable
    con el de reventa y distorsiona la mediana. También excluye avisos
    publicados hace más de `stale_days`: si algo no se vende en tanto
    tiempo probablemente algo lo saca del precio normal (para bien o para
    mal) y no debería fijar qué es "precio de mercado". Ojo: esto NO los
    saca de `find_undervalued` — un aviso viejo *y* barato es justo la
    oportunidad de negociación que el proyecto busca, solo no debe ser él
    mismo quien define la mediana contra la que se lo compara.
    """
    trim = cfg.get("outlier_trim_pct", 5)
    min_n = cfg.get("min_comparables", 20)
    stale_days = cfg.get("stale_days", 60)

    resale = df[~df["off_plan"]] if "off_plan" in df.columns else df
    if "first_seen" in resale.columns:
        first_seen = pd.to_datetime(resale["first_seen"], format="ISO8601", utc=True)
        days_listed = (datetime.now(timezone.utc) - first_seen).dt.days
        resale = resale[days_listed <= stale_days]

    rows = []
    for (zone, rooms), group in resale.groupby(["zone", "rooms"], dropna=False):
        clean = _trim_outliers(group["price_per_m2"], trim)
        if len(clean) < min_n:
            continue
        rows.append({
            "zone": zone,
            "rooms": rooms,
            "n": len(clean),
            "p25": clean.quantile(0.25),
            "median": clean.median(),
            "p75": clean.quantile(0.75),
        })

    return pd.DataFrame(rows)


def find_undervalued(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Avisos cuyo precio/m² está por debajo de la mediana de sus comparables.

    Es un modelo deliberadamente simple. Cuando tengas volumen, reemplazá la
    mediana por una regresión sobre área, ambientes, antigüedad y barrio.
    """
    stats = zone_stats(df, cfg)
    if stats.empty:
        return pd.DataFrame()

    threshold = cfg.get("undervalued_threshold_pct", 15)
    candidates = df[~df["off_plan"]] if "off_plan" in df.columns else df
    merged = candidates.merge(stats[["zone", "rooms", "median", "n"]], on=["zone", "rooms"])
    merged["expected_price"] = merged["median"] * merged["area"]
    merged["discount_pct"] = (
        (merged["expected_price"] - merged["price_norm"]) / merged["expected_price"] * 100
    )

    hits = merged[merged["discount_pct"] >= threshold]
    return hits.sort_values("discount_pct", ascending=False)


def price_drops(conn: sqlite3.Connection) -> pd.DataFrame:
    """Avisos que bajaron de precio, con cuántas veces y cuánto en total.

    Un aviso con varias bajas es un vendedor con apuro. Este dato no existe
    en ningún portal.
    """
    snaps = pd.read_sql_query(
        "SELECT listing_id, seen_at, price_norm FROM price_snapshots "
        "ORDER BY listing_id, seen_at",
        conn,
    )
    if snaps.empty:
        return pd.DataFrame()

    snaps = snaps.dropna(subset=["price_norm"])
    grouped = snaps.groupby("listing_id")["price_norm"]

    summary = pd.DataFrame({
        "first_price": grouped.first(),
        "current_price": grouped.last(),
        "observations": grouped.count(),
    })
    summary["drops"] = grouped.apply(lambda s: int((s.diff() < 0).sum()))
    summary["total_drop_pct"] = (
        (summary["first_price"] - summary["current_price"]) / summary["first_price"] * 100
    )

    result = summary[summary["total_drop_pct"] > 0].reset_index()
    return result.sort_values("total_drop_pct", ascending=False)


def stale_listings(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Avisos publicados hace más de analysis.stale_days."""
    days = cfg.get("stale_days", 60)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    first_seen = pd.to_datetime(df["first_seen"], format="ISO8601", utc=True)
    out = df[first_seen < cutoff].copy()
    out["days_listed"] = (datetime.now(timezone.utc) - first_seen[out.index]).dt.days
    return out.sort_values("days_listed", ascending=False)


def duplicate_candidates(df: pd.DataFrame, title_similarity: float = 0.6) -> pd.DataFrame:
    """Mismo inmueble publicado más de una vez: revela el margen entre agencias.

    El fingerprint solo (zona + ambientes + área + precio) genera muchísimos
    falsos positivos: en Almagro hay cientos de 2 ambientes de 50 m² a 115k que
    no son el mismo departamento. Por eso exigimos además que los títulos se
    parezcan. Son *candidatos*: la confirmación final es mirar las fotos.
    """
    counts = df.groupby("fingerprint").size()
    repeated = counts[counts > 1].index
    dupes = df[df["fingerprint"].isin(repeated)].copy()
    if dupes.empty:
        return dupes

    keep_groups = []
    for fp, group in dupes.groupby("fingerprint"):
        titles = group["title"].fillna("").tolist()
        pairs = [
            SequenceMatcher(None, a.lower(), b.lower()).ratio()
            for i, a in enumerate(titles)
            for b in titles[i + 1:]
        ]
        if pairs and max(pairs) >= title_similarity:
            keep_groups.append(fp)

    dupes = dupes[dupes["fingerprint"].isin(keep_groups)]
    if dupes.empty:
        return dupes

    spread = dupes.groupby("fingerprint")["price_norm"].agg(["min", "max"])
    spread["spread_pct"] = (spread["max"] - spread["min"]) / spread["min"] * 100
    return dupes.merge(spread, on="fingerprint").sort_values(
        ["spread_pct", "fingerprint"], ascending=False
    )


def opportunity_score(df: pd.DataFrame, drops: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Score 0-100 combinando descuento, bajas de precio y antigüedad del aviso.

    Los pesos son un punto de partida: movelos cuando veas resultados reales.
    """
    if df.empty:
        return df

    scored = df.copy()
    threshold = cfg.get("undervalued_threshold_pct", 15)

    discount = scored.get("discount_pct", pd.Series(0, index=scored.index)).fillna(0)
    scored["score_discount"] = (discount / (threshold * 2)).clip(0, 1) * 60

    if not drops.empty:
        drop_map = drops.set_index("listing_id")["total_drop_pct"]
        scored["drop_pct"] = scored["id"].map(drop_map).fillna(0)
    else:
        scored["drop_pct"] = 0
    scored["score_drop"] = (scored["drop_pct"] / 15).clip(0, 1) * 25

    first_seen = pd.to_datetime(scored["first_seen"], format="ISO8601", utc=True)
    days = (datetime.now(timezone.utc) - first_seen).dt.days
    scored["score_stale"] = (days / cfg.get("stale_days", 60)).clip(0, 1) * 15

    scored["score"] = (
        scored["score_discount"] + scored["score_drop"] + scored["score_stale"]
    ).round(1)
    return scored.sort_values("score", ascending=False)
