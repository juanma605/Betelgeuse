"""CLI.

    python -m inmobot scrape    # trae avisos y guarda snapshot de precios
    python -m inmobot analyze   # estadísticas de mercado y oportunidades
    python -m inmobot export    # vuelca resultados a CSV
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from . import analyze, db, normalize
from .config import load as load_config
from .sources import argenprop, mercadolibre, zonaprop

log = logging.getLogger("inmobot")

# La consola de Windows no siempre usa UTF-8 por default: sin esto, los
# títulos/zonas con tildes o ñ salen como "�" en la tabla que imprime
# pandas (no es un bug de pandas, es la codepage de la terminal).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SOURCE_BUILDERS = {
    "mercadolibre": mercadolibre.build,
    "zonaprop": zonaprop.build,
    "argenprop": argenprop.build,
}


def cmd_scrape(cfg) -> None:
    search_cfg = cfg["search"]
    dedup_cfg = cfg["dedup"]
    keep = cfg.get_path("storage.keep_snapshots", True)

    with db.connect(cfg.get_path("storage.path")) as conn:
        for name, conf in cfg["sources"].items():
            if not conf.get("enabled"):
                continue
            builder = SOURCE_BUILDERS.get(name)
            if builder is None:
                log.warning("Fuente '%s' habilitada pero no implementada todavía.", name)
                continue

            source = builder(conf)
            kept: list[dict] = []
            seen_ids: set[str] = set()
            rejected = 0

            for zone in search_cfg["zones"]:
                log.info("[%s] buscando en %s...", name, zone)
                for item in source.fetch(zone, search_cfg):
                    item = normalize.normalize(item, search_cfg, dedup_cfg)
                    ok, _reason = normalize.passes_filters(item, search_cfg)
                    if not ok:
                        rejected += 1
                        continue
                    kept.append(item)
                    seen_ids.add(item["id"])

            stats = db.upsert_listings(conn, kept, keep_snapshots=keep)

            # Si una zona se cortó a mitad de camino (bloqueo anti-bot, 403,
            # etc.) sus avisos reales no van a estar en seen_ids — no hay que
            # darlos de baja como si el aviso hubiera desaparecido de verdad.
            incomplete = getattr(source, "incomplete_zones", set())
            complete_zones = [z for z in search_cfg["zones"] if z not in incomplete]
            gone = db.mark_inactive(conn, seen_ids, name, zones=complete_zones)
            if incomplete:
                log.info(
                    "[%s] zona(s) incompleta(s), no se dan de baja avisos ahí: %s",
                    name, ", ".join(sorted(incomplete)),
                )
            log.info(
                "[%s] %d nuevos, %d actualizados, %d cambios de precio, "
                "%d dados de baja, %d descartados por filtros",
                name, stats["new"], stats["updated"], stats["price_changes"],
                gone, rejected,
            )


def cmd_analyze(cfg, export_dir: Path | None = None) -> None:
    acfg = cfg["analysis"]

    with db.connect(cfg.get_path("storage.path")) as conn:
        df = analyze.load_active(conn)
        if df.empty:
            log.warning("No hay avisos activos. Corré 'scrape' primero.")
            return

        drops = analyze.price_drops(conn)

    stats = analyze.zone_stats(df, acfg)
    under = analyze.find_undervalued(df, acfg)
    stale = analyze.stale_listings(df, acfg)
    dupes = analyze.duplicate_candidates(df)
    scored = analyze.opportunity_score(under if not under.empty else df, drops, acfg)

    print(f"\n{len(df)} avisos activos\n")

    if not stats.empty:
        print("=== Precio por m² (mediana, en moneda de comparación) ===")
        print(stats.round(0).to_string(index=False), "\n")

    if not under.empty:
        print(f"=== Subvaluados (>= {acfg['undervalued_threshold_pct']}% bajo la mediana) ===")
        cols = ["title", "zone", "area", "price_norm", "discount_pct", "url"]
        print(under[cols].head(15).round(1).to_string(index=False), "\n")

    if not drops.empty:
        print("=== Bajaron de precio ===")
        print(drops.head(15).round(1).to_string(index=False), "\n")

    if not stale.empty:
        print(f"=== Publicados hace más de {acfg['stale_days']} días ===")
        print(stale[["title", "zone", "price_norm", "days_listed"]].head(15).to_string(index=False), "\n")

    if not dupes.empty:
        print("=== Mismo inmueble en varias publicaciones ===")
        print(dupes[["fingerprint", "title", "price_norm", "spread_pct"]].head(15).round(1).to_string(index=False), "\n")

    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        for name, frame in [
            ("zone_stats", stats), ("undervalued", under), ("price_drops", drops),
            ("stale", stale), ("duplicates", dupes), ("scored", scored),
        ]:
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                frame.to_csv(export_dir / f"{name}.csv", index=False)
        log.info("CSVs escritos en %s", export_dir)


def main() -> None:
    parser = argparse.ArgumentParser(prog="inmobot")
    parser.add_argument("command", choices=["scrape", "analyze", "export"])
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-o", "--out", default="data/reports")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logging.basicConfig(
        level=cfg.get_path("logging.level", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "scrape":
        cmd_scrape(cfg)
    elif args.command == "analyze":
        cmd_analyze(cfg)
    else:
        cmd_analyze(cfg, export_dir=Path(args.out))


if __name__ == "__main__":
    main()
