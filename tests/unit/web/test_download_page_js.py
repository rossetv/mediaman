"""What the emailed download page does when Download is pressed and while it waits."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

from mediaman.web.responses import respond_failure

HARNESS = Path(__file__).parent / "js/download_page_harness.js"
TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
_SUBMIT = TEMPLATES.parent / "routes/download/submit.py"
_ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
_TRAILER = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
_VOID = frozenset({"meta", "link", "img", "input", "br"})

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


class _Spec(HTMLParser):
    """A page's body as the nested arrays the harness builds its DOM from."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[list] = [["root", {}]]

    def handle_starttag(self, tag, attrs):
        node = [tag, {k: v or "" for k, v in attrs}]
        self.stack[-1].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].append([tag, {k: v or "" for k, v in attrs}])

    def handle_endtag(self, tag):
        while len(self.stack) > 1 and self.stack.pop()[0] != tag:
            pass

    def handle_data(self, data):
        self.stack[-1].append(data)


def _page(**item: object) -> list:
    item = {
        "title": "Film",
        "media_type": "movie",
        "tmdb_id": 7,
        "action": "download",
        "download_state": None,
        "notify_by_email": False,
        "trailer_key": "dQw4w9WgXcQ",
        **item,
    }
    html = _ENV.get_template("download.html").render(
        state="confirm",
        item=item,
        token="t",
        poll_token="p" if item["download_state"] == "queued" else None,
    )
    parser = _Spec()
    parser.feed(html[html.index("<main") :])
    return parser.stack[0][2:]


def _run(scenario: str, *args: str, **item: object) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario, *args],
        input=json.dumps(_page(**item)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _failures() -> dict[str, tuple[int, dict]]:
    """Every failure the server answers a press with, by its words."""
    said = re.findall(
        r'respond_failure\(\s*((?:"[^"]*"\s*)+),\s*status=(\d+)', _SUBMIT.read_text("utf-8")
    )
    found = {}
    for literals, status in said:
        message = "".join(re.findall(r'"([^"]*)"', literals))
        found[message] = (
            int(status),
            json.loads(respond_failure(message, status=int(status)).body),
        )
    return found


@pytest.fixture(scope="module")
def answers() -> dict:
    return _run("answers", json.dumps(_failures()), notify_by_email=True)


@pytest.fixture(scope="module")
def run() -> dict:
    return _run("run")


@pytest.fixture(scope="module")
def late() -> dict:
    return _run("late", download_state="queued", media_type="tv")


def test_a_title_already_on_its_way_gets_its_progress_and_not_a_failure(answers: dict) -> None:
    seen = answers["queued"]

    assert seen["step"] == "Searching" and not seen["alert"]
    assert "already on its way" in seen["words"]
    assert len(seen["polls"]) == 1 and "poll_token=p3" in seen["polls"][0]


def test_a_title_already_on_its_way_promises_the_email_the_answer_says_is_to_come(
    answers: dict,
) -> None:
    assert "send you a mail" in answers["queuedMailed"]["words"]
    assert "send you a mail" not in answers["queued"]["words"]


@pytest.mark.parametrize(
    ("answer", "said", "links"),
    [
        ("inLibrary", "Already in the library", ["/watch#title=tmdb-movie-7", _TRAILER]),
        ("tracked", "Already on the list", [_TRAILER]),
    ],
)
def test_a_title_already_there_is_said_where_the_button_was_and_is_no_failure(
    answers: dict, answer: str, said: str, links: list[str]
) -> None:
    seen = answers[answer]

    assert said in seen["words"] and not seen["alert"]
    assert seen["links"] == links and "Download" not in seen["buttons"]
    assert seen["focus"] == said


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ("unanswered", "Could not reach mediaman."),
        ("The download service didn't take it", "Try again in a moment."),
        ("Downloads aren't set up", "Try again in a moment."),
        ("Too many requests", "Try again in a moment."),
    ],
)
def test_a_failure_worth_retrying_keeps_a_button_and_the_focus(
    answers: dict, answer: str, why: str
) -> None:
    seen = answers[answer]

    assert f"The download could not start. {why}" in seen["words"] and seen["alert"]
    assert seen["buttons"] == ["Try again"] and seen["busy"] is None
    assert seen["focus"] == "Try again"


def test_a_link_that_cannot_work_again_offers_no_retry(answers: dict) -> None:
    for answer, said in (
        ("Token expired or invalid", "This link does not work any more."),
        ("This download link has already been used", "been used already"),
        ("The download service doesn't know this show", "Ask whoever sent you this link."),
        ("No TVDB ID found for this series", "Ask whoever sent you this link."),
    ):
        seen = answers[answer]
        assert said in seen["words"] and seen["alert"]
        assert seen["buttons"] == ["Reload this page"] and seen["links"] == [""]
        assert seen["focus"] == "Reload this page"


def test_no_failure_the_server_words_for_an_admin_reaches_a_guest(answers: dict) -> None:
    failures = _failures()
    assert {status for status, _ in failures.values()} >= {404, 409, 410, 422, 429, 502, 503}

    for message in failures:
        words = answers[message]["words"]
        assert message not in words
        assert not re.search(r"arr\b|TMDB|TVDB|admin|connectivity", words, re.I), words


def test_a_refusal_that_gives_no_reason_does_not_say_to_try_again(answers: dict) -> None:
    seen = answers["wordless"]

    assert seen["alert"] and "could not start" in seen["words"]
    assert "again" not in seen["words"]


_BUSY = {
    "label": "Adding…",
    "busy": "true",
    "focused": True,
    "spinner": True,
    "minWidth": "132px",
}


def test_pressing_keeps_the_focus_sends_once_and_can_be_tried_again() -> None:
    out = _run("retry")

    assert out["first"] == _BUSY
    assert out["failed"]["posted"] == 1
    assert out["second"] == _BUSY
    assert out["done"]["posted"] == 2 and out["to"] == "/download/t"
    assert out["done"]["step"] == "Searching"
    assert all("poll_token=p2" in url for url in out["polls"])


def test_the_press_leaves_the_title_and_puts_the_steps_in_the_button_s_place(run: dict) -> None:
    seen = run["searching"]

    assert seen["title"] == "Film" and seen["step"] == "Searching"
    assert "Download" not in seen["buttons"] and not seen["bar"]
    assert seen["focus"] and seen["focus"].startswith("Asked for")


def test_the_steps_the_bar_and_the_colour_follow_the_download(run: dict) -> None:
    steps = {k: (v["step"], v["bar"], v["said"]) for k, v in run.items() if v["step"]}

    assert steps == {
        "searching": ("Searching", False, "Searching"),
        "queued": ("Queued", False, "Queued"),
        "searchedAgain": ("Searching", False, "Searching"),
        "downloading": ("Downloading", True, "Downloading"),
        "later": ("Downloading", True, "Downloading"),
        "unanswered": ("Downloading", True, "Downloading"),
        "almost": ("Almost ready", False, "Almost ready"),
        "last": ("Ready", False, "Ready to watch"),
    }
    assert run["downloading"]["figures"] == "34% 5 min"
    assert run["later"]["figures"] == "80%"
    colours = [run[k]["colour"] for k in ("searching", "downloading", "later", "almost", "last")]
    assert colours == sorted(set(colours)) and colours[-1] == 1


def test_a_bar_that_shuts_keeps_its_figures(run: dict) -> None:
    assert run["almost"]["figures"] == "80%"


def test_a_service_that_does_not_answer_leaves_the_last_progress_drawn(run: dict) -> None:
    assert run["unanswered"] == run["later"]


def test_a_title_that_becomes_ready_ends_with_the_way_to_watch_it(run: dict) -> None:
    seen = run["ready"]

    assert "Ready to watch" in seen["words"]
    assert seen["buttons"] == ["Watch on mediaman"]
    assert seen["links"] == ["/watch#title=tmdb-movie-7"]
    assert seen["focus"] == "Ready to watch"


def test_a_show_is_watched_by_its_own_address() -> None:
    ready = _run("run", media_type="tv", trailer_key=None)["ready"]

    assert ready["links"] == ["/watch#title=tmdb-tv-7"]


def test_a_title_tmdb_does_not_name_is_watched_on_the_watch_page() -> None:
    assert _run("run", tmdb_id=None)["ready"]["links"] == ["/watch"]


def test_the_page_shows_an_eta_that_arrives_on_a_later_poll() -> None:
    out = _run("eta", download_state="queued")

    assert out == {
        "start": "",
        "noEtaYet": "34%",
        "arrives": "34% 5 min",
        "changes": "34% 4 min",
        "clears": "34%",
    }


def test_the_wide_backdrop_stays_behind_the_title_after_the_press() -> None:
    wide = "https://image.tmdb.org/t/p/w1280/wide.jpg"
    poster = "https://image.tmdb.org/t/p/w500/poster.jpg"
    out = _run("pictures", backdrop_url=wide, poster_url=poster)

    assert out["before"] == out["after"] == [wide, poster, poster]


def test_only_a_link_with_an_email_promises_one(run: dict) -> None:
    promised = _run("run", notify_by_email=True)["searching"]["words"]

    assert "send you a mail" in promised and "send you a mail" not in run["searching"]["words"]


def test_the_episodes_come_go_and_stay_as_the_person_left_them() -> None:
    out = _run("episodes", download_state="queued", media_type="tv")

    assert out["none"]
    assert out["first"] == {
        "shown": True,
        "open": False,
        "summary": "2 episodes",
        "rows": ["Queued", "Searching"],
    }
    assert out["after"] == {
        "shown": True,
        "open": True,
        "summary": "3 episodes",
        "rows": ["Ready", "Searching", "Queued"],
    }
    (closing, opening) = out["eased"]
    assert closing == opening[::-1] and closing[0] == {"height": "20px"}
    assert set(closing[1].values()) == {0} and {"paddingTop", "marginTop"} <= set(closing[1])
    assert out["shut"] == {"open": False, "held": ""}
    assert out["opened"] == {"open": True, "held": "flex-start"}
    assert out["narrowed"] == "flex-start" and out["letGo"] == ""
    assert out["gone"] == {"shown": False, "focus": "OL"}
    assert out["back"]["shown"] and out["back"]["open"] and out["back"]["rows"] == ["Queued"]


def test_a_ready_title_takes_no_more_answers(late: dict) -> None:
    assert late["ready"]["step"] == "Ready" and late["ready"]["timers"] == 1
    assert "Updates have stopped" not in late["ready"]["words"]
    assert late["after"]["buttons"] == ["Watch on mediaman"]


def test_what_becomes_ready_unasked_takes_no_focus(late: dict) -> None:
    assert late["after"]["focus"] is None


def test_an_answer_that_a_newer_one_overtook_is_not_drawn(late: dict) -> None:
    assert late["overtaken"]["figures"].startswith("60%")
    assert "Updates have stopped" not in late["overtaken"]["words"]


def test_when_updates_stop_the_last_progress_stays_and_a_reload_is_offered(late: dict) -> None:
    seen = late["stopped"]

    assert "Updates have stopped. Reload the page" in seen["words"]
    assert "You can close this page" not in seen["words"]
    assert seen["figures"].startswith("34%") and not seen["polling"]


@pytest.fixture(scope="module")
def trailer() -> dict:
    return _run("trailer")


@pytest.fixture(scope="module")
def twice() -> dict:
    return _run("twice")


def test_youtube_is_not_asked_until_the_trailer_is_pressed(trailer: dict) -> None:
    assert trailer["before"]["frames"] == [] and trailer["newTab"]["frames"] == []


def test_the_trailer_plays_sandboxed_from_the_no_cookie_origin(trailer: dict) -> None:
    (frame,) = trailer["on"]["frames"]

    assert frame["src"].startswith("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?rel=0")
    assert frame["referrerpolicy"] == "strict-origin"
    # popups let the player's own "Watch on YouTube" link open, outside the sandbox
    assert set(frame["sandbox"].split()) == {
        "allow-scripts",
        "allow-same-origin",
        "allow-presentation",
        "allow-popups",
        "allow-popups-to-escape-sandbox",
    }


def test_the_trailer_takes_the_focus_and_gives_it_back(trailer: dict) -> None:
    assert trailer["on"]["inert"] and trailer["on"]["focus"] == "Close trailer"
    assert "watch-btn--grey" in trailer["fading"]["focus"]
    off = trailer["off"]
    assert off["frames"] == [] and not off["on"] and not off["inert"]
    assert "watch-btn--grey" in off["focus"]


def test_buttons_clear_of_the_player_stay_of_use_and_those_under_it_do_not(trailer: dict) -> None:
    assert trailer["on"]["inert"] and not trailer["upright"]["inert"]
    assert trailer["sideways"]["inert"]


def test_focus_the_player_comes_to_lie_over_goes_to_its_close_button(trailer: dict) -> None:
    assert trailer["sideways"]["focus"] == "Close trailer"


def test_a_resize_with_no_trailer_on_leaves_the_buttons_be(trailer: dict) -> None:
    assert not trailer["offResized"]["inert"]


def test_a_trailer_whose_button_is_gone_gives_the_focus_to_the_title(trailer: dict) -> None:
    assert trailer["gone"]["tag"] == "H1" and not trailer["gone"]["on"]


def test_a_trailer_pressed_further_down_the_page_is_brought_into_view(trailer: dict) -> None:
    hero, block, behavior = trailer["scrolled"]

    assert "share-hero" in hero and (block, behavior) == ("start", "auto")


def test_the_button_stops_the_trailer_it_started(trailer: dict) -> None:
    assert not trailer["again"]["on"] and trailer["again"]["frames"] == []
    assert trailer["told"] == ["pauseVideo"]


def test_back_closes_the_trailer_and_forward_opens_it(trailer: dict) -> None:
    assert trailer["entries"] == 2
    assert not trailer["back"]["on"] and trailer["back"]["frames"] == []
    assert trailer["forward"]["on"] and len(trailer["forward"]["frames"]) == 1


def test_a_trailer_that_ends_closes_by_its_own_player_s_word_alone(trailer: dict) -> None:
    assert trailer["played"]["on"] and not trailer["ended"]["on"]


def test_the_video_fades_out_before_it_goes(trailer: dict) -> None:
    assert not trailer["fading"]["on"] and len(trailer["fading"]["frames"]) == 1
    assert trailer["faded"]["frames"] == []


def test_a_reload_under_a_trailer_leaves_no_trailer_s_entry(trailer: dict) -> None:
    assert trailer["reloaded"] == {"state": None, "on": False, "left": True}


def test_a_page_come_back_to_past_its_trailer_lets_the_browser_keep_its_place(
    trailer: dict,
) -> None:
    assert trailer["returned"] == "auto"


@pytest.mark.parametrize("way", ["close", "button", "escape", "end"])
def test_closing_the_trailer_twice_at_once_never_leaves_the_page(way: str, twice: dict) -> None:
    assert twice[way] == {
        "left": False,
        "on": False,
        "barHeld": "none",
        "barAfter": "",
        "opens": True,
        "back": [False, False],
    }


def test_enter_held_on_close_does_not_go_on_to_the_trailer_button(twice: dict) -> None:
    assert twice["enter"] == {"held": True, "typed": False}
