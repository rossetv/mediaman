"""mediaman's name is one drawing: each page shows it once, as a link heard as "mediaman"."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

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


class _Hidden(HTMLParser):
    """Where each link goes that aria-hidden takes from a screen reader."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str | None] = []
        self._open: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        hidden = found.get("aria-hidden") == "true" or any(h for _, h in self._open)
        if tag == "a" and hidden:
            self.links.append(found.get("href"))
        self._open.append((tag, hidden))

    def handle_endtag(self, tag):
        # What has no end, such as <input>, goes with what holds it.
        while self._open and self._open.pop()[0] != tag:
            pass


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
    hidden = _Hidden()
    hidden.feed(html)
    assert hidden.links == []
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
        ("_nav.html", ("/", "mediaman, home")),
        ("keep.html", ("/", "mediaman")),
        ("download.html", ("/", "mediaman")),
        ("subscribers/unsubscribe_confirm.html", ("/", "mediaman")),
        ("subscribers/unsubscribe_result.html", ("/", "mediaman")),
    ],
)
def test_a_template_shows_the_name_once_as_a_link(name, link):
    assert _links(_TEMPLATES.get_template(name).render(state="limited")) == [link]


def test_the_name_is_drawn_and_never_spelt_out_in_two_colours():
    for path in [*(_WEB / "templates").rglob("*.html"), *(_WEB / "static" / "js").rglob("*.js")]:
        assert not re.search(r"media<(b|span|em)\b", path.read_text()), path.name


def test_the_tv_shows_the_same_drawing_by_itself_under_a_name_that_is_heard():
    html = _tv_page()
    picture = re.search(r"<svg([^>]*)>(.*?)</svg>", html, flags=re.S)

    assert 'role="img"' in picture[1] and 'aria-label="mediaman"' in picture[1]
    assert picture[2].strip() == _DRAWING
    # An old TV's browser knows no colour from a custom property, and no symbol by its href.
    assert "var(" not in _DRAWING and "<use" not in html


def _tv_page() -> str:
    return _TEMPLATES.get_template("tv.html").render()


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


def test_a_failed_sign_in_shows_its_error_beside_a_name_that_stands_still():
    html = _TEMPLATES.get_template("login.html").render(error="Wrong")
    assert "Wrong" in html and "login-brand" in html
    assert "login-brand--drawn" not in html


def _tv_css(opening: str) -> str:
    """What stands in the TV page's one block that starts as named."""
    css = (_WEB / "static" / "css" / "tv.css").read_text()
    return re.search(re.escape(opening) + r"[^{]*\{\n(.*?)\n\}", css, flags=re.S)[1]


def test_the_tv_waits_with_the_icon_s_own_two_arches():
    waits = re.search(r'<div class="spin spin--mark">(.*?)</div>', _tv_page(), flags=re.S)[1]
    svg = "{http://www.w3.org/2000/svg}"
    icon = ElementTree.parse(_WEB / "static" / "icons" / "favicon.svg").getroot()
    large = next(g for g in icon.iter(f"{svg}g") if g.get("class") == "large")

    assert re.findall(r' d="([^"]*)"', waits) == [p.get("d") for p in large.iter(f"{svg}path")]
    assert 'aria-hidden="true"' in waits


def test_the_tv_s_icon_takes_the_ring_s_place_only_where_a_stroke_is_known_to_animate():
    css = (_WEB / "static" / "css" / "tv.css").read_text()
    drawn = _tv_css("@supports (stroke-dashoffset: 1px) and (animation-name: a1)")

    assert ".mark { display: none; }" in css.split("@supports")[0]
    assert ".mark { display: block;" in drawn
    assert re.search(r"\.spin--mark \{[^}]*border: 0;[^}]*animation: none;", drawn)
    assert "animation: a1 1.8s linear infinite" in drawn
    assert "animation: a2 1.8s .3s linear infinite" in drawn


def test_the_tv_s_icon_stands_still_and_whole_under_reduced_motion():
    css = (_WEB / "static" / "css" / "tv.css").read_text()
    assert css.index("@supports") < css.index("prefers-reduced-motion")
    assert ".spin--mark, .mark path { animation: none; }" in _tv_css("@media (prefers-reduced")


def test_the_tv_s_qr_code_stays_black_on_white_where_colours_are_forced():
    forced = _tv_css("@media (forced-colors")
    assert re.search(r"\.qr\b[^{}]*\{[^}]*forced-color-adjust: none;", forced)


def test_the_second_half_keeps_the_page_s_blue_and_takes_the_text_colour_where_colours_are_forced():
    tokens = (_WEB / "static" / "css" / "_tokens.css").read_text()
    blue = re.search(r"--accent:\s*(#\w+);", tokens)[1]
    assert f'class="wm-man" fill="none" stroke="{blue}"' in _DRAWING
    tv = (_WEB / "static" / "css" / "tv.css").read_text()
    assert ".wm-man { stroke: currentColor; }" in tv.split("forced-colors: active")[1]
