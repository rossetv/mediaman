"""The connection announce region sits once per card, outside the role="button" header."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import jinja2

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
_VOID = {"input", "br", "img", "hr", "meta", "link"}


class _Regions(HTMLParser):
    """Collect each live region with the open elements around it."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[dict[str, str | None]] = []
        self.regions: list[list[dict[str, str | None]]] = []
        self.cards = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        self.stack.append({"tag": tag, **a})
        if "intg-card" in (a.get("class") or "").split():
            self.cards += 1
        if "data-conn-announce" in a:
            self.regions.append(list(self.stack))

    def handle_endtag(self, tag):
        if tag not in _VOID and self.stack:
            self.stack.pop()


def _parse(source: str) -> _Regions:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    parser = _Regions()
    parser.feed(env.from_string(source).render())
    return parser


def test_pill_has_no_live_region():
    html = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES)).from_string(
        '{% from "_components.html" import conn_pill %}{{ conn_pill("plex") }}'
    ).render()
    assert "data-conn-label>Untested</span>" in html
    assert "role=" not in html


def test_each_card_has_one_status_region_outside_the_button_header():
    parsed = _parse(
        '{% from "_components.html" import intg_card %}'
        '{% call intg_card("sonarr", "S", "a", "Sonarr") %}x{% endcall %}'
        '{% call intg_card("radarr", "R", "b", "Radarr") %}y{% endcall %}'
    )
    assert parsed.cards == 2
    assert len(parsed.regions) == 2
    for chain in parsed.regions:
        region = chain[-1]
        assert region["role"] == "status"
        assert not any(el.get("role") == "button" for el in chain[:-1])
        assert "intg-card" in (chain[-2].get("class") or "").split()
