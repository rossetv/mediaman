"""mediaman's name is one drawing: each page shows it once, as a link heard as "mediaman"."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.app_factory import create_app

_WEB = Path(mediaman.web.__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB / "templates"))
_DRAWING = _TEMPLATES.get_template("_wordmark.html").render().strip()
_FROM_SPRITE = '<use href="#i-wordmark"/>'


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(), headers={"Accept": "text/html"})


def _links(html: str) -> list[tuple[str, str]]:
    """Each link that shows the name: where it goes and what a screen reader hears."""
    body = re.sub(r'<svg class="ico-sprite".*?</svg>', "", html, flags=re.S)
    shown = body.count(_FROM_SPRITE) + body.count(_DRAWING)
    links = [
        (re.search(r'href="([^"]*)"', tag)[1], re.search(r'aria-label="([^"]*)"', tag)[1])
        for tag, inner in re.findall(r"(<a\b[^>]*>)(.*?)</a>", body, flags=re.S)
        if _FROM_SPRITE in inner or _DRAWING in inner
    ]
    assert shown == len(links)
    return links


@pytest.mark.parametrize(
    ("path", "link"),
    [
        ("/login", ("/", "mediaman")),
        ("/no/such/page", ("/", "mediaman")),
    ],
)
def test_a_page_of_the_app_shows_the_name_once_as_a_link(client, path, link):
    assert _links(client.get(path).text) == [link]


@pytest.mark.parametrize(
    ("name", "link"),
    [
        ("force_password_change.html", ("/", "mediaman")),
        ("_nav.html", ("/watch", "mediaman, home")),
        ("keep.html", ("/watch", "mediaman")),
        ("download.html", ("/watch", "mediaman")),
        ("subscribers/unsubscribe_confirm.html", ("/watch", "mediaman")),
        ("subscribers/unsubscribe_result.html", ("/watch", "mediaman")),
    ],
)
def test_a_template_shows_the_name_once_as_a_link(name, link):
    assert _links(_TEMPLATES.get_template(name).render(state="limited")) == [link]


def test_the_name_is_drawn_and_never_spelt_out_in_two_colours():
    for path in [*(_WEB / "templates").rglob("*.html"), *(_WEB / "static" / "js").rglob("*.js")]:
        assert not re.search(r"media<(b|span|em)\b", path.read_text()), path.name


def test_the_tv_shows_the_same_drawing_by_itself_under_a_name_that_is_heard():
    html = _TEMPLATES.get_template("tv.html").render()
    picture = re.search(r"<svg([^>]*)>(.*?)</svg>", html, flags=re.S)

    assert 'role="img"' in picture[1] and 'aria-label="mediaman"' in picture[1]
    assert picture[2].strip() == _DRAWING
    # An old TV's browser knows no colour from a custom property, and no symbol by its href.
    assert "var(" not in _DRAWING and "<use" not in html


def test_every_drawing_of_the_name_has_the_same_box():
    sources = ("_icons.html", "login.html", "tv.html")
    boxes = {
        box
        for name in sources
        for box in re.findall(
            r'viewBox="([^"]*)"[^>]*>\{% include "_wordmark.html" %\}',
            (_WEB / "templates" / name).read_text(),
        )
    }
    assert boxes == {"0 -735 4210 970"}


def test_the_name_draws_itself_on_the_sign_in_page_alone_and_not_under_reduced_motion(client):
    css = (_WEB / "static" / "css" / "_login.css").read_text()
    moving = re.search(
        r"@media \(prefers-reduced-motion: no-preference\) \{\n(.*?)\n\}", css, flags=re.S
    )[0]
    rules = re.findall(r"\.login-brand--drawn [^{]*\{[^}]*\}", css)

    assert len(rules) == 3 and all(rule in moving for rule in rules)
    assert not any("infinite" in rule for rule in rules)
    assert "login-brand--drawn" in client.get("/login").text
    assert "login-brand--drawn" not in client.get("/no/such/page").text


def test_the_second_half_keeps_the_page_s_blue_and_takes_the_text_colour_where_colours_are_forced():
    tokens = (_WEB / "static" / "css" / "_tokens.css").read_text()
    blue = re.search(r"--accent:\s*(#\w+);", tokens)[1]
    assert f'class="wm-man" fill="none" stroke="{blue}"' in _DRAWING
    tv = (_WEB / "static" / "css" / "tv.css").read_text()
    assert ".wm-man { stroke: currentColor; }" in tv.split("forced-colors: active")[1]
