"""Utilidades compartidas para fuentes que necesitan renderizar JS (Playwright).

Zonaprop y Argenprop no tienen API pública: hay que abrir la página como un
navegador real. Un solo lugar para el setup del browser (headers, user-agent)
para no repetirlo en cada fuente.
"""

from __future__ import annotations

from contextlib import contextmanager

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def is_bot_challenge(page) -> bool:
    """Detecta la pantalla de verificación de Cloudflare ("Un momento...").

    Zonaprop y Argenprop la disparan típicamente a partir de la 2da
    navegación dentro de la misma sesión headless, incluso respetando el
    rate limit. No la esquivamos (no es el objetivo del proyecto arriesgar
    un ban de IP por más velocidad) — solo la reconocemos para cortar la
    paginación con un mensaje claro en vez de un timeout genérico.
    """
    try:
        title = (page.title() or "").lower()
        body = page.inner_text("body")[:500].lower()
    except Exception:
        return False
    return "verificación de seguridad" in body or "un momento" in title


@contextmanager
def browser_page():
    """Contexto con una página de Chromium headless lista para navegar."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="es-AR",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()
