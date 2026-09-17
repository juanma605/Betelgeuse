"""Carga de configuración.

Un solo YAML manda sobre todo el pipeline. Acá lo leemos, aplicamos defaults
y resolvemos los valores del tipo "env:NOMBRE_DE_VARIABLE".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "search": {
        "zones": [],
        "currency": "USD",
        "price_min": None,
        "price_max": None,
        "filters": {},
        "fx_rates": {"ARS_per_USD": None},
    },
    "sources": {},
    "storage": {"path": "data/listings.db", "keep_snapshots": True},
    "dedup": {"enabled": True, "area_tolerance_m2": 2, "price_tolerance_pct": 5},
    "analysis": {
        "undervalued_threshold_pct": 15,
        "min_comparables": 20,
        "stale_days": 60,
        "outlier_trim_pct": 5,
    },
    "alerts": {},
    "logging": {"level": "INFO"},
}


class Config(dict):
    """dict con acceso por ruta punteada: cfg.get_path('analysis.stale_days')."""

    def get_path(self, path: str, default: Any = None) -> Any:
        node: Any = self
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_env(node: Any) -> Any:
    """Convierte "env:FOO" en el valor de la variable de entorno FOO."""
    if isinstance(node, dict):
        return {k: _resolve_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env(v) for v in node]
    if isinstance(node, str) and node.startswith("env:"):
        return os.environ.get(node[4:])
    return node


def load(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No encuentro {path}. Copiá config.yaml de ejemplo.")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(DEFAULTS, raw)
    cfg = Config(_resolve_env(merged))
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not cfg.get_path("search.zones"):
        raise ValueError("search.zones está vacío: no hay nada que buscar.")

    pmin = cfg.get_path("search.price_min")
    pmax = cfg.get_path("search.price_max")
    if pmin is not None and pmax is not None and pmin > pmax:
        raise ValueError("search.price_min es mayor que search.price_max.")

    enabled = [
        name
        for name, conf in (cfg.get_path("sources", {}) or {}).items()
        if isinstance(conf, dict) and conf.get("enabled")
    ]
    if not enabled:
        raise ValueError("No hay ninguna fuente habilitada en sources.")
