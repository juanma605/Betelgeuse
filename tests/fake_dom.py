"""Un ElementHandle de Playwright de mentira, hecho con BeautifulSoup.

Los `_map()` de las fuentes reciben un elemento de Playwright. Para probarlos
contra un fixture HTML no hace falta levantar un navegador: alcanza con imitar
los cuatro métodos que usan. Es chico a propósito — si una fuente empieza a
usar más API de Playwright, este archivo tiene que crecer con ella y eso se
nota enseguida.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"


class FakeElement:
    def __init__(self, node):
        self._node = node

    def get_attribute(self, name: str) -> str | None:
        value = self._node.get(name)
        # BeautifulSoup devuelve class="a b" como lista.
        return " ".join(value) if isinstance(value, list) else value

    def query_selector(self, selector: str) -> "FakeElement | None":
        found = self._node.select_one(selector)
        return FakeElement(found) if found else None

    def query_selector_all(self, selector: str) -> list["FakeElement"]:
        return [FakeElement(node) for node in self._node.select(selector)]

    def inner_text(self) -> str:
        """Aproximación de innerText: sin la indentación del HTML y con un
        salto de línea entre nodos, que es como se ve el texto renderizado."""
        return self._node.get_text("\n", strip=True)

    def evaluate(self, expression: str):
        """El único evaluate() que usan las fuentes: si el carrusel hermano
        de la card de Mudafy tiene una foto."""
        if "parentElement" in expression and "img" in expression:
            parent = self._node.parent
            return bool(parent and parent.select_one("img"))
        raise NotImplementedError(expression)


def card_from(filename: str, selector: str) -> FakeElement:
    html = (FIXTURES / filename).read_text(encoding="utf-8")
    node = BeautifulSoup(html, "html.parser").select_one(selector)
    assert node is not None, f"{filename} no matchea el selector '{selector}'"
    return FakeElement(node)
