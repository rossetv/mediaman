"""Every icon the pages draw is a symbol of the one inline sprite, so none needs a web font."""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest

WEB = Path(__file__).resolve().parents[3] / "src" / "mediaman" / "web"
TEMPLATES = WEB / "templates"
SPRITE = TEMPLATES / "_icons.html"
SOURCES = [
    p
    for p in sorted([*TEMPLATES.rglob("*.html"), *(WEB / "static" / "js").rglob("*.js")])
    if p != SPRITE
]
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)


def _symbols() -> list[str]:
    return re.findall(r'<symbol id="([^"]+)"', SPRITE.read_text(encoding="utf-8"))


def test_every_icon_named_has_a_symbol_and_every_symbol_is_named() -> None:
    named = {
        name
        for p in SOURCES
        for name in re.findall(r"""["'](i-[a-z0-9-]+)["']""", p.read_text(encoding="utf-8"))
    }
    assert named == set(_symbols())
    assert len(_symbols()) == len(set(_symbols()))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_an_icon_is_drawn_by_the_name_of_a_symbol(path: Path) -> None:
    # A misspelt or unprefixed name draws nothing, and nothing says so.
    text = path.read_text(encoding="utf-8")
    drawn = re.findall(r"""\bicon\(\s*["']([^"']*)["']""", text)
    assert set(drawn) <= set(_symbols())
    # The calls that take an icon's name in a later argument.
    calls = re.findall(r"\b(?:setIcon|scanLabel|button|note|statePill|showLine)\((.*)", text)
    bare = {name.removeprefix("i-") for name in _symbols()}
    assert not bare & {s for c in calls for s in re.findall(r"""["']([a-z-]+)["']""", c)}
    # A name put together at run time escapes both checks.
    assert not re.search(r"""["']i-["']""", text)


def test_no_page_links_the_icon_font_or_draws_with_its_classes() -> None:
    for p in SOURCES + sorted((WEB / "static" / "css").glob("*.css")):
        text = p.read_text(encoding="utf-8")
        assert "fontawesome" not in text.lower(), p.name
        assert not re.search(r"\bfa-[a-z]", text), p.name
        assert "@font-face" not in text, p.name


def test_the_page_carries_the_sprite_once_before_its_first_icon() -> None:
    page = ENV.get_template("watch_player.html").render(
        item={"rating_key": "1", "title": "Film"}, back_href="/watch", next=None, leaving=None
    )
    assert page.count('class="ico-sprite"') == 1
    assert page.index('class="ico-sprite"') < page.index('<svg class="ico"')


def test_every_page_gets_the_sprite_from_the_base_page() -> None:
    for p in TEMPLATES.rglob("*.html"):
        text = p.read_text(encoding="utf-8")
        # The TV's page draws its own: the browsers of old TVs cannot use a symbol by its href.
        if "<html" in text and p.name != "tv.html":
            assert '{% include "_icons.html" %}' in text, p.name
