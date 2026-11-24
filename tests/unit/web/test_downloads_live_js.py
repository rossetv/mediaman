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
