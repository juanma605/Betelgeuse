"""CLI.

    python -m inmobot scrape        # trae avisos y guarda snapshot de precios
    python -m inmobot analyze       # estadísticas de mercado y oportunidades
    python -m inmobot export        # vuelca resultados a CSV
    python -m inmobot demo-export   # copia anonimizada de la base para el repo

`analyze` y `export` aceptan `--demo`: corren contra data/demo.db en vez de
la base real, sin red ni credenciales.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from . import alerts, analyze, db, demo, normalize
from .config import load as load_config
from .sources import argenprop, mercadolibre, mudafy, remax, zonaprop

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
    "mudafy": mudafy.build,
    "remax": remax.build,
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


def cmd_analyze(cfg, export_dir: Path | None = None, use_demo: bool = False) -> None:
    acfg = cfg["analysis"]

    with db.connect(demo.db_path(cfg, use_demo)) as conn:
        df = analyze.load_active(conn)
        if df.empty:
            log.warning("No hay avisos activos. Corré 'scrape' primero.")
            return

        drops = analyze.price_drops(conn)
        history = analyze.history_note(conn)

    stats = analyze.zone_stats(df, acfg)
    under = analyze.find_undervalued(df, acfg)
    stale = analyze.stale_listings(df, acfg)
    # Los títulos del demo son sintéticos y salen de los mismos campos que el
    # fingerprint: compararlos daría siempre ~1 de similitud.
    dupes = analyze.duplicate_candidates(df, compare_titles=not use_demo)
    scored = analyze.opportunity_score(under if not under.empty else df, drops, acfg)

    print(f"\n{len(df)} avisos activos{' (dataset de demo, anonimizado)' if use_demo else ''}\n")

    print("=== Precio por m² (mediana, en moneda de comparación) ===")
    if stats.empty:
        print(analyze.comparables_note(df, acfg), "\n")
    else:
        print(stats.round(0).to_string(index=False), "\n")

    print(f"=== Subvaluados (>= {acfg['undervalued_threshold_pct']}% bajo la mediana) ===")
    if not under.empty:
        cols = ["title", "zone", "area", "price_norm", "discount_pct", "url"]
        print(under[cols].head(15).round(1).to_string(index=False), "\n")
    elif stats.empty:
        print("Sin medianas de referencia no hay contra qué comparar.\n")
    else:
        print("Ningún aviso quedó por debajo del umbral.\n")

    print("=== Bajaron de precio ===")
    if not drops.empty:
        print(drops.head(15).round(1).to_string(index=False), "\n")
    else:
        print(history or "Ningún aviso bajó de precio todavía.", "\n")

    if not stale.empty:
        print(f"=== Publicados hace más de {acfg['stale_days']} días ===")
        print(stale[["title", "zone", "price_norm", "days_listed"]].head(15).to_string(index=False), "\n")

    if not dupes.empty:
        print("=== Mismo inmueble en varias publicaciones ===")
        if use_demo:
            print("(solo por fingerprint: sin títulos reales no se puede filtrar por similitud)")
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

    # El demo tiene que correr sin red: no se manda mail sobre datos viejos
    # y anonimizados de una base que no es la del que lo está probando.
    if not use_demo:
        sent = alerts.send_email_alerts(scored, cfg.get_path("alerts.email", {}) or {})
        if sent:
            log.info("Alerta por mail: %d aviso(s) por encima del score mínimo.", sent)


def cmd_demo_export(cfg, out: str, limit: int | None) -> None:
    stats = demo.export(cfg.get_path("storage.path"), out, limit)
    log.info(
        "Demo escrito en %s: %d avisos de %d zonas, %d snapshots de precio.",
        out, stats["listings"], stats["zones"], stats["snapshots"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="inmobot")
    parser.add_argument("command", choices=["scrape", "analyze", "export", "demo-export"])
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-o", "--out", default=None)
    parser.add_argument(
        "--demo", action="store_true",
        help="analizar data/demo.db en vez de la base real (sin red ni credenciales)",
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="tope aproximado de avisos a exportar en demo-export",
    )
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
        cmd_analyze(cfg, use_demo=args.demo)
    elif args.command == "demo-export":
        cmd_demo_export(cfg, args.out or demo.DEMO_DB_PATH, args.limit)
    else:
        cmd_analyze(cfg, export_dir=Path(args.out or "data/reports"), use_demo=args.demo)


if __name__ == "__main__":
    main()
