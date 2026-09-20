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
import time
from pathlib import Path

import pandas as pd

from . import alerts, analyze, db, demo, geocode, normalize
from .config import DEFAULTS
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

    enabled = [n for n, c in cfg["sources"].items() if c.get("enabled")]
    log.info(
        "=== Scrape: %d fuentes (%s) x %d zonas ===",
        len(enabled), ", ".join(enabled), len(search_cfg["zones"]),
    )
    started = time.monotonic()
    totals = {"new": 0, "updated": 0, "price_changes": 0}

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
            for key in totals:
                totals[key] += stats[key]

        geo_cfg = cfg.get_path("geocoding", {}) or {}
        if geo_cfg.get("enabled"):
            ubic = geocode.completar_coordenadas(conn, geo_cfg)
            log.info(
                "[geocode] %d avisos ubicados por su dirección (%d direcciones nuevas "
                "consultadas, %d sin resultado, %d avisos siguen sin ubicación)",
                ubic["ubicados"], ubic["consultadas"], ubic["sin_resultado"],
                ubic["pendientes"],
            )

    log.info(
        "=== Terminado en %.0f min: %d nuevos, %d actualizados, %d cambios de precio. ===",
        (time.monotonic() - started) / 60,
        totals["new"], totals["updated"], totals["price_changes"],
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
        top = under.head(15).round(1)
        top = top.assign(area=[
            f"{a:g}*" if est else f"{a:g}" for a, est in zip(top["area"], top["area_estimada"])
        ])
        cols = ["title", "zone", "area", "price_norm", "discount_pct", "url"]
        print(top[cols].to_string(index=False))
        if top["area_estimada"].any():
            print(
                "* m² totales, no cubiertos: el aviso no publica la superficie "
                "cubierta, así que su descuento sale inflado (puede ser un PH con "
                "patio o un depto con balcón grande). Revisalo antes de creerlo."
            )
        print()
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

    try:
        cfg = load_config(args.config)
    except Exception:
        # Un error de tipeo en el YAML no puede frenar el scrape de las 7 sin
        # dejar rastro: se loguea con el archivo por defecto.
        _setup_logging("INFO", DEFAULTS["logging"]["file"])
        log.exception("No pude cargar %s", args.config)
        sys.exit(1)
    _setup_logging(cfg.get_path("logging.level", "INFO"), cfg.get_path("logging.file"))

    try:
        if args.command == "scrape":
            cmd_scrape(cfg)
        elif args.command == "analyze":
            cmd_analyze(cfg, use_demo=args.demo)
        elif args.command == "demo-export":
            cmd_demo_export(cfg, args.out or demo.DEMO_DB_PATH, args.limit)
        else:
            cmd_analyze(cfg, export_dir=Path(args.out or "data/reports"), use_demo=args.demo)
    except Exception:
        log.exception("'%s' se cortó por un error", args.command)
        sys.exit(1)


def _setup_logging(level: str, log_file: str | None) -> None:
    """A la pantalla y al archivo a la vez.

    Antes el .bat mandaba toda la salida al archivo y la ventana quedaba en
    blanco aunque estuviera trabajando. Va a stdout y no a stderr (el default
    de logging) para que se vea igual que un print.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error = None
    if log_file:
        # Si otra corrida tiene el archivo tomado (Windows lo bloquea), el
        # scrape sigue igual, solo en pantalla: el log nunca lo puede frenar.
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            file_error = exc
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%d/%m %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # httpx loguea cada request en INFO: una línea por página, puro ruido.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if file_error:
        log.warning("No puedo escribir en %s (%s): esta corrida sale solo en pantalla.",
                    log_file, file_error)


if __name__ == "__main__":
    main()
