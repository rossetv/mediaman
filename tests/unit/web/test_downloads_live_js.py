"""What the /downloads page makes of the polls after the server drew it: the real templates,
read by the real scripts under node.
"""

from __future__ import annotations

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
HARNESS = Path(__file__).parent / "js" / "downloads_live_harness.js"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB / "templates"), autoescape=True)
_VOID = {"img", "input", "link", "meta", "br", "use"}

ITEM = {
    "id": "h",
    "title": "Th",
    "media_type": "movie",
    "poster_url": "",
    "state": "downloading",
    "state_label": "Downloading",
    "progress": 0,
    "eta": "",
    "size_done": "",
    "size_total": "",
    "episodes": None,
    "episode_summary": "",
    "search_hint": "",
    "arr_link": "",
    "arr_source": "",
    "abandon_visible": False,
    "stuck_seasons": [],
    "release_label": "",
}


class _Spec(HTMLParser):
    """The markup as nested [tag, attrs, ...children] lists."""

    def __init__(self) -> None:
        super().__init__()
        self.root: list = ["root", {}]
        self._open = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: list = [tag, {k: v or "" for k, v in attrs}]
        self._open[-1].append(node)
        if tag not in _VOID:
            self._open.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID:
            assert self._open.pop()[0] == tag

    def handle_data(self, data: str) -> None:
        self._open[-1].append(data)


def _item(dl_id: str, **over: object) -> dict[str, object]:
    return {**ITEM, "id": dl_id, "title": f"T{dl_id}", **over}


def _html(**data: object) -> str:
    base = {"hero": None, "queue": [], "upcoming": [], "recent": [], "subtitle": "", "outage": ""}
    template = _ENV.get_template("downloads.html")
    context = template.new_context({**base, "can_abandon": True, **data})
    return "".join(template.blocks["content"](context))


def _run(scenario: str, **data: object) -> dict:
    parser = _Spec()
    parser.feed(_html(**data))
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        input=json.dumps(parser.root[2:]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_row_drawn_before_its_download_begins_shows_the_percentage_and_time_left() -> None:
    out = _run("rowBegins", hero=_item("h"), queue=[_item("q")])
    assert out["begun"] == ["37%", "~4 min"]
    assert out["lit"]
    assert out["back"] == ["—", ""] and out["quietAgain"]


_EPISODE = {
    "label": "S01E01",
    "title": "Title S01E01",
    "state": "downloading",
    "progress": 10,
    "is_pack_episode": False,
}


def test_a_queued_shows_episodes_follow_each_poll_and_an_open_list_stays_open() -> None:
    row = _item("q", media_type="series", episodes=[_EPISODE, {**_EPISODE, "label": "S01E09"}])
    out = _run("rowEpisodes", hero=_item("h"), queue=[row])
    assert out["first"] == {
        "rows": ["S01E01|Title S01E01||20%"],
        "toggle": "1 episode",
        "count": "1 episode",
        "open": True,
    }
    assert out["more"] == {
        "rows": ["S01E01|Title S01E01||Ready", "S01E02|Named late||Queued"],
        "toggle": "2 episodes",
        "count": "2 episodes",
        "open": True,
    }
    assert out["pack"]["rows"] == ["S01E01|Title S01E01|In pack", "S01E02|Title S01E02|In pack"]
    assert out["pack"]["toggle"] == "2 episodes · pack download"
    assert out["none"] == {"rows": [], "toggle": None, "count": "", "open": False}


def test_a_row_drawn_without_episodes_gains_them() -> None:
    out = _run("rowEpisodes", hero=_item("h"), queue=[_item("q", media_type="series")])
    assert out["more"]["rows"] == ["S01E01|Title S01E01||Ready", "S01E02|Named late||Queued"]
    assert out["more"]["count"] == "2 episodes"


def test_an_episodes_button_added_by_a_poll_says_whether_it_is_open() -> None:
    out = _run("heroEpisodes", hero=_item("h", media_type="series"))
    assert out == {"drawn": "false", "opened": "true", "inInfo": True}


def test_every_bar_says_whose_progress_it_is_and_every_section_has_a_heading() -> None:
    show = _item("h", progress=10, media_type="series", episodes=[{**_EPISODE, "progress": 5}])
    out = _run("spoken", hero=show, queue=[_item("q", progress=1)])
    assert out["drawn"] == [["Th", "10"], ["S01E01", "5"], ["Tq", "1"]]
    assert out["first"]["bars"] == [["Th2", "20"], ["S01E01", "10"], ["Tq", "2"]]
    assert out["rebuilt"] == [["Th3", "30"], ["S01E01", "15"], ["Tq", "3"]]
    headings = ["Th2", "Also downloading", "Coming soon", "Recently added"]
    assert out["first"]["headings"] == ["H1 Downloads", *(f"H2 {h}" for h in headings)]


def test_the_server_draws_the_same_headings_as_the_polls() -> None:
    data = {
        "hero": _item("h"),
        "queue": [_item("q")],
        "upcoming": [_item("u")],
        "recent": [_item("r")],
    }
    out = _run("spoken", **data)
    assert [h[:2] for h in out["first"]["headings"]] == ["H1", "H2", "H2", "H2", "H2"]


def test_the_hero_never_wears_the_art_of_the_title_before_it() -> None:
    poster = "https://art/h.jpg"
    out = _run("art", hero=_item("h", poster_url=poster), queue=[_item("q")])
    assert out["drawn"] == [poster, f'url("{poster}")']
    assert out["kept"], "an unchanged poster is not loaded again"
    assert out["bare"]["art"] == [None, None]
    assert out["bare"]["pill"] == "TV"
    assert out["next"] == ["https://art/c.jpg", 'url("https://art/c.jpg")']


def test_a_row_gains_a_poster_that_comes_late() -> None:
    out = _run("art", hero=_item("h", poster_url="https://art/h.jpg"), queue=[_item("q")])
    assert out["bare"]["row"] == "https://art/q.jpg"


def test_a_poster_that_fails_to_load_leaves_no_broken_icon_anywhere_on_the_page() -> None:
    art = {"poster_url": "https://art/0.jpg"}
    out = _run(
        "broken",
        hero=_item("h", **art),
        queue=[_item("q", **art)],
        upcoming=[_item("u", **art)],
        recent=[_item("r", **art)],
    )
    assert out == {"drawn": [True] * 4, "fresh": [False] * 4, "polled": [True] * 4}


def test_what_is_coming_soon_gives_way_to_what_replaces_it() -> None:
    out = _run("upcomingReplaced", hero=_item("h"), upcoming=[_item("u")])
    assert out == {"rows": ["v"]}


def test_a_link_to_radarr_keeps_the_focus_while_its_line_changes() -> None:
    searching = {
        "state": "searching",
        "search_hint": "Searched once",
        "arr_link": "http://arr/a",
        "arr_source": "Radarr",
    }
    out = _run("hintFocus", hero=_item("h", **searching), queue=[_item("q", **searching)])
    followed = [
        ["http://arr/b", "Searched 2× · next attempt in 3m · View in Radarr ↗"],
        ["http://arr/c", "View in Radarr ↗"],
    ]
    assert out["hero"] == out["row"] == followed
    assert out["noLink"] == [True, True]
    assert out["built"] == "Searched once · View in Radarr ↗"


def test_a_page_that_hears_nothing_says_so_and_goes_on_asking() -> None:
    out = _run("lost", hero=_item("h"), recent=[_item("r")], subtitle="1 item in progress")
    # subtitle, dimmed cards, recently added titles
    live = ["1 item in progress", 0, 1]
    cut_off = ["1 item in progress · can't reach mediaman", 1, 1]
    assert out["once"] == live, "one failed poll is no news"
    assert out["twice"] == cut_off
    assert out["back"] == live
    assert out["late"] == cut_off
    assert out["retried"], "a poll that hangs is given up and another sent"
    assert out["backAgain"] == live
    # a poll the hidden tab was waiting for does not hold up the next
    assert out["shownAgain"] == [7, 0]
