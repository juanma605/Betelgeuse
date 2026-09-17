"""Alertas por mail.

Manda un solo mail por corrida con los avisos que superan
`alerts.email.min_score`. Nada de dependencias nuevas: smtplib alcanza.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import pandas as pd

log = logging.getLogger(__name__)


def send_email_alerts(scored: pd.DataFrame, email_cfg: dict) -> int:
    """Manda el mail si hay avisos por encima de min_score. Devuelve cuántos."""
    if not email_cfg.get("enabled"):
        return 0
    if scored is None or scored.empty or "score" not in scored.columns:
        return 0

    min_score = email_cfg.get("min_score", 70)
    hits = scored[scored["score"] >= min_score]
    if hits.empty:
        return 0

    host = email_cfg.get("smtp_host")
    to_addrs = email_cfg.get("to_addrs") or []
    if not host or not to_addrs:
        log.warning(
            "alerts.email está habilitado pero falta smtp_host o to_addrs — no mando nada."
        )
        return 0

    port = int(email_cfg.get("smtp_port", 587))
    user = email_cfg.get("smtp_user")
    password = email_cfg.get("smtp_password")
    from_addr = email_cfg.get("from_addr") or user

    msg = MIMEText(_format_body(hits, min_score), "plain", "utf-8")
    msg["Subject"] = f"inmobot: {len(hits)} oportunidad(es) con score >= {min_score}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, to_addrs, msg.as_string())

    log.info("Alerta enviada por mail: %d aviso(s) con score >= %d.", len(hits), min_score)
    return len(hits)


def _format_body(hits: pd.DataFrame, min_score: float) -> str:
    lines = [f"{len(hits)} aviso(s) con score >= {min_score}:\n"]
    for _, row in hits.sort_values("score", ascending=False).iterrows():
        title = str(row.get("title") or "")[:80]
        lines.append(
            f"- [{row.get('score')}] {title} — {row.get('zone')} — "
            f"{row.get('price_norm')} — {row.get('url')}"
        )
    return "\n".join(lines)
