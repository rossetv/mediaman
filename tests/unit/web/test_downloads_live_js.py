"""What the /downloads page makes of the polls after the server drew it: the real templates,
read by the real scripts under node.
"""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2

from tests.unit.web._css import px, rgb, rules, style, token

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
        "inline": 0,
        "inside": 2,
        "open": True,
    }
    assert out["more"] == {
        "rows": ["S01E01|Title S01E01||Ready", "S01E02|Named late||Queued"],
        "toggle": "2 episodes",
        "inline": 0,
        "inside": 2,
        "open": True,
    }
    assert out["pack"]["rows"] == ["S01E01|Title S01E01|In pack", "S01E02|Title S01E02|In pack"]
    assert out["pack"]["toggle"] == "2 episodes · pack download"
    assert out["none"] == {"rows": [], "toggle": None, "inline": 0, "inside": 0, "open": False}


def test_a_row_drawn_without_episodes_gains_them() -> None:
    out = _run("rowEpisodes", hero=_item("h"), queue=[_item("q", media_type="series")])
    assert out["more"]["rows"] == ["S01E01|Title S01E01||Ready", "S01E02|Named late||Queued"]
    assert out["more"]["toggle"] == "2 episodes" and out["more"]["inline"] == 0
    # in the row's card, under its text, as the server draws them
    assert out["more"]["inside"] == 2


def test_an_episodes_button_added_by_a_poll_says_whether_it_is_open() -> None:
    out = _run("heroEpisodes", hero=_item("h", media_type="series"))
    assert out == {"drawn": "false", "opened": "true", "inInfo": True}


def test_every_bar_says_whose_progress_it_is_and_every_section_has_a_heading() -> None:
    show = _item("h", progress=10, media_type="series", episodes=[{**_EPISODE, "progress": 5}])
    out = _run("spoken", hero=show, queue=[_item("q", progress=1)])
    assert out["drawn"] == [["Th", "10"], ["S01E01", "5"], ["Tq", "1"]]
    assert out["first"]["bars"] == [["Th2", "20"], ["S01E01", "10"], ["Tq", "2"]]
    assert out["rebuilt"] == [["Th3", "30"], ["S01E01", "15"], ["Tq", "3"]]
    assert out["brought"] == [["S01E01", "7"], ["S01E02", "7"]]
    headings = ["Th2", "Also in progress", "Coming soon", "Recently added"]
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
    out = _run(
        "lost", hero=_item("h", eta="5 min"), recent=[_item("r")], subtitle="1 item in progress"
    )
    # subtitle, the top card and its time left, recently added titles: what is shown stays
    live = ["1 item in progress", 1, 1, "5 min"]
    cut_off = ["1 item in progress · can't reach mediaman", 1, 1, "5 min"]
    assert out["once"] == live, "one failed poll is no news"
    assert out["twice"] == cut_off
    assert out["back"] == live
    assert out["late"] == cut_off
    assert out["retried"], "a poll that hangs is given up and another sent"
    assert out["backAgain"] == live
    # a poll the hidden tab was waiting for does not hold up the next
    assert out["shownAgain"] == [7, 1]


def test_after_an_abandon_the_page_says_so_and_the_focus_is_on_something() -> None:
    stuck = {"state": "searching", "abandon_visible": True}
    out = _run(
        "abandoned",
        hero=_item("h", **stuck),
        queue=[_item("q1", **stuck), _item("q2", **stuck)],
        upcoming=[_item("u")],
    )
    assert out["on"] == ["q2", "H1", "H1", "H1", "q4"]
    assert [variant for _, variant in out["toasts"]] == ["success"] * 5
    for (text, _), title in zip(out["toasts"], ["Tq1", "Tq2", "Th", "Tu", "Tq3"], strict=True):
        assert title in text


SHEET = "_dl.css"
COARSE = "(pointer: coarse)"


def _rule(selector: str, media: str | None = None) -> dict[str, str]:
    return style(selector, SHEET, media)


def test_the_abandon_dialog_keeps_the_width_of_the_page_s_scrollbar_until_it_closes() -> None:
    stuck = {"state": "searching", "abandon_visible": True}
    out = _run("held", queue=[_item("q1", **stuck)])
    assert out == {"open": "15px", "again": "15px", "closed": None}


def test_the_hero_cannot_be_scrolled_inside_itself() -> None:
    assert _rule(".dl-hero")["overflow"] == "clip"


def _within(node: list, cls: str) -> list[list]:
    """Every element under *node* whose class list holds *cls*."""
    found = []
    for child in node[2:]:
        if isinstance(child, list):
            if cls in child[1].get("class", "").split():
                found.append(child)
            found += _within(child, cls)
    return found


def _marks(node: list) -> set[str]:
    """The data-v of every element under *node*."""
    marks = set()
    for child in node[2:]:
        if isinstance(child, list):
            marks |= {child[1].get("data-v", "")} | _marks(child)
    return marks


def test_a_rows_episodes_button_and_list_are_inside_its_card_under_its_text() -> None:
    parser = _Spec()
    parser.feed(_html(queue=[_item("q", media_type="series", episodes=[_EPISODE])]))
    [card] = _within(parser.root, "dl-compact-row")
    assert {"ep-toggle", "ep-list"} <= _marks(card)
    poster_column = _rule(".dl-compact-row")["grid-template-columns"].split()[0]
    assert poster_column.endswith("px"), "the first column is the poster's, the text the second"
    for part in (
        ".dl-compact-row > .dl-compact-episodes-toggle",
        ".dl-compact-row > .dl-compact-episodes",
    ):
        assert _rule(part)["grid-column"] == "2 / -1", part
    assert _rule(".dl-fold.is-folded")["margin-top"] == "0"


def test_the_state_of_a_download_is_not_in_the_accent_itself_on_its_own_tint() -> None:
    for pill in (".dl-state-downloading", ".dl-ep-status-pill.downloading"):
        assert _rule(pill)["color"] == "var(--accent-text)", pill


def test_a_finger_reaches_every_control_at_any_width() -> None:
    assert px(_rule(".dl-arr-link", COARSE)["min-height"]) >= 44
    # In two pieces each would be reached over its own line alone.
    assert _rule(".dl-arr-link")["display"] == "inline-block"
    assert _rule(".dl-arr-link")["white-space"] == "nowrap"
    for toggle in (".dl-hero-episodes-toggle", ".dl-compact-episodes-toggle"):
        assert px(_rule(toggle, COARSE)["min-height"]) >= 44, toggle
    for button in (".dl-hero-abandon .btn.btn--icon", ".dl-row-abandon .btn.btn--icon"):
        sized = _rule(button, COARSE)
        assert min(px(sized["width"]), px(sized["height"])) >= 44, button


def test_a_link_on_a_line_of_its_own_has_no_dot_before_it() -> None:
    phone = "(max-width: 479.98px)"
    assert _rule(".dl-arr-link", phone)["display"] == "flex"
    assert _rule(".dl-hint-sep", phone)["display"] == "none"
    out = _run("hintFocus", hero=_item("h", **_STUCK), queue=[_item("q", **_STUCK)])
    assert out["dots"] == 2, "in the row the server drew and the one a poll built"


def test_queue_rows_sit_one_gap_apart() -> None:
    assert not [prop for prop in _rule(".dl-compact") if prop.startswith("margin")]
    # a folded row takes the gap after it away with it, or the rows below jump as it goes
    gap = px(_rule(".dl-compact-list")["gap"])
    assert gap > 0 and gap == -px(_rule(".dl-compact-list > .dl-fold.is-folded")["margin-bottom"])


def test_the_small_state_pills_are_large_enough_to_read_and_a_poll_leaves_them_so() -> None:
    sizes = [_rule(".dl-state-pill")["font-size"], _rule(".dl-ep-status-pill")["font-size"]]
    sizes += [
        d["font-size"] for _, s, d in rules(SHEET) if ".dl-state-pill--xs" in s and "font-size" in d
    ]
    assert all(px(size) >= 11 for size in sizes), sizes
    assert _run("pollLook", hero=_item("h"), queue=[_item("q")])["pill"] == ""


def test_a_new_hero_shows_through_the_one_before_it() -> None:
    out = _run("pollLookMoving", hero=_item("h", abandon_visible=True, **_STUCK))
    assert out["same"] == 0, "the same title does not fade"
    # what fades out is the old card, with nothing on it to find, press or read out
    assert out["first"] == [{"says": "Th", "inert": True, "hidden": "true", "hooks": 0}]
    assert out["second"] == ["Tb"], "one at a time"
    assert out["after"] == 0 and out["hero"] == "Tc"


def test_a_poll_while_the_old_hero_fades_draws_the_new_one_in_full() -> None:
    old = _item("h", abandon_visible=True, episode_summary="2 ready", **_STUCK)
    out = _run("pollDuringFade", hero=old)
    assert out["bare"] == [False, False]
    assert out["full"] == [True, ["1 searching"]]
    assert out["fading"] == 1 and out["old"] == ["2 ready"], "the copy stays as it was"


def test_with_reduced_motion_a_new_hero_takes_the_place_of_the_old_at_once() -> None:
    out = _run("pollLook", hero=_item("h"), queue=[_item("q")])
    assert out["first"] == out["second"] == []


def test_the_heros_background_may_come_from_mediaman_itself() -> None:
    out = _run("ownArt", hero=_item("h", poster_url="/api/watch/img/tmdb/w342/h.jpg"))
    assert out["drawn"] == 'url("/api/watch/img/tmdb/w342/h.jpg")'
    assert out["other"] == [None] * 3, "a browser reads these as another host"


_STUCK = {
    "state": "searching",
    "search_hint": "x",
    "arr_link": "http://arr/h",
    "arr_source": "Radarr",
}


def test_a_row_that_changes_place_keeps_the_focus_in_it() -> None:
    show = _item("q2", media_type="series", episodes=[_EPISODE], **_STUCK)
    rows = [_item("q1", **_STUCK), show]
    out = _run("reordered", hero=_item("h"), queue=rows)
    assert out == {"rows": ["q2", "q1"], "on": True}


def test_a_control_of_the_hero_never_becomes_another_titles_under_the_focus() -> None:
    show = _item("h", media_type="series", episodes=[_EPISODE], abandon_visible=True, **_STUCK)
    out = _run("heroFollowed", hero=show)
    # it stays through a poll of the same title, and leaves with the title
    assert out["on"] == [[True, "H1"]] * 3
    assert out["closed"] == [False, "false", False]


def test_a_poll_that_cannot_be_drawn_is_not_told_as_mediaman_not_answering() -> None:
    out = _run("unreadable", hero=_item("h"), subtitle="1 item in progress")
    assert out == {"polls": 3, "logged": 3, "line": "1 item in progress", "dimmed": 0}


def test_what_is_coming_soon_gains_and_changes_its_poster() -> None:
    out = _run("upcomingPoster", hero=_item("h"), upcoming=[_item("u")])
    assert out["out"] == [[], ["https://art/1.jpg"], ["https://art/2.jpg"], []]


def test_a_small_state_pill_stays_on_one_line() -> None:
    assert _rule(".dl-state-pill--xs")["white-space"] == "nowrap"


def test_the_hero_eases_to_the_height_of_the_title_that_takes_its_place() -> None:
    out = _run("heroHeightMoving", hero=_item("h"))
    assert out["first"] == ["", "500px", "407px"]
    # a swap while it moves goes on from where it is, and the first one's end is not its end
    assert out["second"] == ["", "450px", "460px"]
    assert out["afterFirstTimer"] == "460px" and out["settled"] == ""
    assert out["same"] == 0
    eased = {
        name: timing
        for name, _, timing in (
            p.strip().partition(" ") for p in _rule(".dl-hero")["transition"].split(",")
        )
    }
    assert eased.get("height", "").split()[:1] == ["var(--dur-slow)"], "it eases, not snaps"
    assert float(token("--dur-slow")[:-1]) > 0


def test_with_reduced_motion_the_hero_is_at_once_as_tall_as_its_title() -> None:
    out = _run("heroHeight", hero=_item("h"))
    assert out["first"] == out["second"] == [] and out["same"] == 0


def _mix(over: tuple[float, ...], alpha: float, under: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a * alpha + b * (1 - alpha) for a, b in zip(over, under, strict=True))


def _contrast(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    def light(colour: tuple[float, ...]) -> float:
        lin = [c / 255 / 12.92 if c <= 10 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in colour]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    high, low = sorted((light(a), light(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_text_of_the_hero_is_read_over_a_white_poster() -> None:
    white, black = (255.0,) * 3, (0.0,) * 3
    art = float(_rule(".dl-hero-bg")["opacity"])
    scrim_paint = _rule(".dl-hero-scrim")["background"]
    scrim = min(float(a) for a in re.findall(r"rgba\(0,0,0,([.\d]+)\)", scrim_paint))
    card = _mix(black, scrim, _mix(white, art, rgb(token("--s2"))))

    def text(name: str) -> tuple[float, ...]:
        value = token(f"--{name}")
        if value.startswith("#"):
            return rgb(value)
        return _mix(white, float(value.split(",")[3].rstrip(")")), card)

    for selector in (
        ".dl-hero-ep-summary",
        ":is(.dl-hero, .dl-hero-ghost) .dl-search-hint",
        ":is(.dl-hero, .dl-hero-ghost) .dl-arr-link",
        ".dl-hero-details",
    ):
        name = re.fullmatch(r"var\(--([\w-]+)\)", _rule(selector)["color"])
        assert name, selector
        assert _contrast(text(name[1]), card) >= 4.5, selector
    pills = _rule(":is(.dl-hero, .dl-hero-ghost) :is(.pill, .dl-state-pill)")["background"]
    base = re.fullmatch(r"color-mix\(in srgb, currentColor (\d+)%, var\(--(\w+)\)\)", pills)
    assert base
    for name in ("accent-text", "purple-text", "orange", "success", "warning"):
        tint = _mix(text(name), int(base[1]) / 100, rgb(token(f"--{base[2]}")))
        assert _contrast(text(name), tint) >= 4.5, name


def test_an_episode_in_a_pack_is_not_faded_with_its_text() -> None:
    pack = [d for _, sels, d in rules(SHEET) if any(".dl-ep-row-pack" in sel for sel in sels)]
    assert pack and not any("opacity" in d for d in pack)


def test_a_failed_abandon_leaves_the_focus_in_the_dialog_whatever_a_click_does_with_it() -> None:
    stuck = {"state": "searching", "abandon_visible": True}
    out = _run("abandonFailsByClick", hero=_item("h", **stuck))
    assert out["during"] and out["after"]
    assert out["back"], "and closing it gives the focus to the button that opened it"
    assert out["posts"] == 1 and out["errors"] == 1
    # a click beside the buttons stays in the dialog too
    assert out["dialog"] == "-1"


def test_on_a_phone_a_hero_that_changes_height_keeps_its_top_where_it_is() -> None:
    phone = "(max-width: 639.98px)"
    for card in (".dl-hero", ".dl-hero-ghost__card"):
        assert _rule(card, phone)["align-items"] == "flex-start", card
    # what is shorter than the card still sits at its foot
    content = _rule(".dl-hero-content", phone)
    assert content["justify-content"] == "flex-end"
    padding = px(_rule(".dl-hero", phone)["padding"])
    assert px(content["min-height"]) == px(_rule(".dl-hero")["min-height"]) - 2 * padding
    assert _rule(".dl-hero-info", phone)["flex"] == "none"
    # the old card's ground goes on under it while the card grows
    assert _rule(".dl-hero-ghost__card")["inset"] == "0"


def test_the_ring_is_told_by_the_page_s_own_polls_and_of_those_that_fail() -> None:
    assert _run("ring") == [1, "lost", 2]
