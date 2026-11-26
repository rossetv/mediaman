"""No stylesheet asks `:has()` of the page itself.

A `:has()` on `html` or `body` is tested again on every change anywhere under it, and each time it
is, every element a `:has()` rule styles is restyled with it: with a few thousand cards on Watch,
opening a title or pointing at a card froze the page. What the page holds is said by a class or
an attribute on `html` instead, set by the template or the script that changes it.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web" / "static" / "css"

# A compound that starts with html, body or :root and has a :has() of its own.
PAGE_HAS = re.compile(r"(?:^|[\s,>+~(])(?:html|body|:root)(?:[.#\[:][^\s,>+~{]*?)?:has\(", re.M)


def _selectors(path: Path) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return [s.strip() for s in re.findall(r"([^{}]+)\{", css) if not s.strip().startswith("@")]


def test_no_stylesheet_has_a_has_on_the_page_itself() -> None:
    found = [
        f"{path.name}: {sel}"
        for path in sorted(CSS_DIR.glob("*.css"))
        for sel in _selectors(path)
        if PAGE_HAS.search(sel)
    ]
    assert found == []


def test_the_check_finds_a_has_on_the_page_and_lets_one_on_a_part_be() -> None:
    assert PAGE_HAS.search("html:not(.has-ttl) body:has(> .x)")
    assert PAGE_HAS.search("html.has-ttl:has(.tv-mini)")
    assert not PAGE_HAS.search(".share-body:has(> .ttl__reason)")
    assert not PAGE_HAS.search(".watch-card__sub:has(> .watch-card__lead)")
