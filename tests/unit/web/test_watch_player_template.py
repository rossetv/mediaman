"""The Watch player page: CSP-safe markup and the context handed to player.js."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

from mediaman.services.media_meta.plex_quality import quality_rows
from mediaman.services.media_meta.plex_watch import PlexItem

TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)

EPISODE = {
    "item": {
        "rating_key": "501",
        "title": "The Long Coast",
        "subtitle": "S2 · E4 — Low Water",
        "duration_s": 3000.0,
    },
    "start_s": 1152.5,
    "back_href": "/watch/show/77?season=2",
    "next": {"href": "/watch/play/502", "label": "S2 · E5 — High Tide"},
    "show_href": "/watch/show/77",
    "leaving": {"days": 2, "keep_id": "88"},
}
LAST_EPISODE = {**EPISODE, "next": None, "leaving": None}
MOVIE = {
    "item": {
        "rating_key": "900",
        "title": "Harbour Lights",
        "subtitle": "2019",
        "duration_s": 6120.0,
    },
    "start_s": 0,
    "back_href": "/watch/movie/900",
    "next": None,
    "show_href": None,
    "leaving": None,
}
CONTEXTS = {"episode": EPISODE, "last_episode": LAST_EPISODE, "movie": MOVIE}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: dict[int, str] = {}
        self._open: list[int] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag in ("button", "a"):
            self._open.append(len(self.elements) - 1)

    def handle_endtag(self, tag):
        if tag in ("button", "a") and self._open:
            self._open.pop()

    def handle_data(self, data):
        for i in self._open:
            self.text[i] = self.text.get(i, "") + data

    def player(self) -> dict[str, str | None]:
        return next(a for t, a in self.elements if a.get("id") == "player")

    def controls(self) -> list[str]:
        """Every button and link, as its label and text."""
        return [
            " ".join(f"{a.get('aria-label') or ''} {self.text.get(i, '')}".split())
            for i, (t, a) in enumerate(self.elements)
            if t in ("button", "a")
        ]


def _render(ctx: dict) -> tuple[str, _Page]:
    html = _ENV.get_template("watch_player.html").render(**ctx)
    page = _Page()
    page.feed(html)
    return html, page


@pytest.mark.parametrize("name", CONTEXTS)
def test_page_has_no_inline_script_or_style(name):
    _, page = _render(CONTEXTS[name])
    inline = [a for t, a in page.elements if t == "script" and not a.get("src")]
    assert inline == []
    assert [t for t, a in page.elements if "style" in a] == []


def test_hls_loads_before_the_player_script():
    _, page = _render(EPISODE)
    srcs = [a["src"] for t, a in page.elements if t == "script" and a.get("src")]
    assert srcs.index("/static/vendor/hls/hls.min.js?v=") < srcs.index("/static/js/player.js?v=")


def test_the_ways_to_a_tv_load_before_the_player_and_googles_only_where_allowed():
    def scripts(**ctx):
        _, page = _render({**EPISODE, **ctx})
        srcs = [a["src"] for t, a in page.elements if t == "script" and a.get("src")]
        return [s.split("/")[-1].split("?")[0] for s in srcs if "/watch/" in s or "player" in s]

    def allowed(on):
        return {"request": SimpleNamespace(state=SimpleNamespace(google_cast=on))}

    ours = ["play-on.js", "airplay.js"]
    assert scripts() == [*ours, "player.js"]
    assert scripts(**allowed(False)) == [*ours, "player.js"]
    assert scripts(**allowed(True)) == [*ours, "cast-sender.js", "player.js"]


def test_nothing_says_there_is_a_tv_before_one_is_known():
    _, page = _render(EPISODE)
    buttons = [a for t, a in page.elements if t == "button" and "data-play-on" in a]
    assert [("hidden" in a) for a in buttons] == [True, True]
    away = [a for t, a in page.elements if "data-away" in a or "data-away-end" in a]
    assert [("hidden" in a) for a in away] == [True, True]


def test_what_rates_a_title_loads_before_the_player_script():
    _, page = _render(MOVIE)
    srcs = [a["src"] for t, a in page.elements if t == "script" and a.get("src")]
    assert srcs.index("/static/js/ratings.js?v=") < srcs.index("/static/js/player.js?v=")
    assert "/static/css/_ratings.css?v=" in [a.get("href") for t, a in page.elements if t == "link"]


@pytest.mark.parametrize("name", CONTEXTS)
def test_data_attributes_carry_the_context(name):
    ctx = CONTEXTS[name]
    _, page = _render(ctx)
    root = page.player()
    assert json.loads(root["data-item"]) == ctx["item"]
    assert float(root["data-start-s"]) == ctx["start_s"]
    assert root["data-back-href"] == ctx["back_href"]
    assert json.loads(root["data-next"]) == ctx["next"]
    assert root["data-show-href"] == (ctx["show_href"] or "")
    assert json.loads(root["data-leaving"]) == ctx["leaving"]


@pytest.mark.parametrize(("place_s", "attribute"), [(0.0, "0.0"), (12.5, "12.5"), (None, "")])
def test_the_place_saved_is_told_apart_from_none(place_s, attribute):
    _, page = _render({**EPISODE, "start_s": 0, "place_s": place_s})
    assert page.player()["data-place-s"] == attribute


@pytest.mark.parametrize(("days", "says"), [(-1, "Leaving any day"), (0, "Leaving today")])
def test_a_date_that_has_passed_is_not_called_today(days, says):
    _, page = _render({**EPISODE, "leaving": {"days": days, "keep_id": "88"}})
    assert any(c.startswith(f"{says} · Keep") for c in page.controls())


@pytest.mark.parametrize("state", ["next", "end", "unknown"])
def test_the_page_says_whether_the_next_episode_is_known(state):
    _, page = _render({**EPISODE, "next_state": state})
    assert page.player()["data-next-state"] == state


def test_hostile_title_stays_data():
    title = """It's "<script>alert(1)</script>" & more"""
    ctx = {**MOVIE, "item": {**MOVIE["item"], "title": title}}
    _, page = _render(ctx)
    assert json.loads(page.player()["data-item"])["title"] == title
    assert [t for t, a in page.elements if t == "script" and not a.get("src")] == []


def test_episode_with_next_and_leaving_shows_both():
    _, page = _render(EPISODE)
    controls = page.controls()
    assert any("Next episode" in c for c in controls)
    assert any("Leaving in 2 days" in c and "Keep" in c for c in controls)
    keep_days = [a["data-keep-days"] for t, a in page.elements if "data-keep-days" in a]
    assert keep_days == ["7", "30", "90"]


@pytest.mark.parametrize("name", ["last_episode", "movie"])
def test_without_next_or_leaving_neither_shows(name):
    _, page = _render(CONTEXTS[name])
    controls = page.controls()
    assert not any("Next episode" in c for c in controls)
    assert not any("Leaving" in c for c in controls)


def test_one_day_left_reads_in_the_singular():
    _, page = _render({**EPISODE, "leaving": {"days": 1, "keep_id": "88"}})
    assert any(c.startswith("Leaving in 1 day ·") for c in page.controls())


@pytest.mark.parametrize("name", CONTEXTS)
def test_there_is_no_audio_or_subtitle_choice(name):
    _, page = _render(CONTEXTS[name])
    labels = " ".join(page.controls()).lower()
    assert "audio" not in labels
    assert "subtitle" not in labels


def test_an_item_without_media_details_offers_the_three_basic_qualities():
    item = PlexItem("episode", "501", "Low Water", "", None, 3000, "2", None, False)
    _, page = _render({**EPISODE, "qualities": quality_rows(item)})
    offered = json.loads(page.player()["data-qualities"])
    assert [r["id"] for r in offered] == ["best", "medium", "low"]


@pytest.mark.parametrize("name", CONTEXTS)
def test_icon_controls_show_no_words_but_carry_a_name_and_a_tooltip(name):
    _, page = _render(CONTEXTS[name])
    icons = [
        (i, a)
        for i, (t, a) in enumerate(page.elements)
        if t in ("button", "a") and {"player-btn", "player-back"} & set(a["class"].split())
    ]
    assert any("data-fullscreen" in a for _, a in icons)
    for i, a in icons:
        assert page.text.get(i, "").strip() == ""
        assert a["title"].startswith(a["aria-label"])


def test_the_next_episode_carries_its_still_and_the_show_s_backdrop_in_its_place():
    next_episode = {"href": "/watch/play/502?from=%2Fwatch", "label": "S2 · E5"}
    _, page = _render({**EPISODE, "next": next_episode})
    still = next(a for t, a in page.elements if t == "img" and "data-still" in a and "src" in a)
    assert still["src"] == "/api/watch/thumb/502"
    assert still["data-fallback"] == "/api/poster/77?kind=art"
    # Nothing of it shows until it is there.
    assert "hidden" in still
