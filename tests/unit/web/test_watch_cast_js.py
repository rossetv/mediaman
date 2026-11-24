"""The people of a title, in its detail, run under node against a fake DOM: the card that a
person's photo opens, what it asks for, and the way to their page."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_cast_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_going_along_the_cast_without_resting_opens_and_asks_for_nothing() -> None:
    assert _run("sweep") == {"asked": [], "card": None}


def test_resting_on_a_person_says_who_they_are_at_once_and_the_rest_when_it_comes() -> None:
    out = _run("rest")
    loading = out["loading"]
    assert (loading["name"], loading["role"], loading["loading"]) == (
        "Mara Ellison",
        "as Part 0",
        True,
    )
    full = out["full"]
    assert (full["born"], full["known"]) == ("Born 1979, age 47", ["Undertow", "Low Water"])
    # What TMDB says is words, never markup.
    assert (full["bio"], out["markup"]) == ("Trained in <b>Leeds</b>.", 0)
    assert out["left"] is None
    assert out["again"] == full


def test_a_person_is_asked_about_once() -> None:
    assert _run("rest")["asked"] == ["9001"]


def test_a_reader_on_a_person_is_told_the_words_of_their_card() -> None:
    described = _run("rest")["described"]
    assert "Part 0" in described and "Born 1979" in described and "Leeds" in described


def test_an_open_card_is_asked_about_though_another_answer_is_on_its_way() -> None:
    out = _run("pass")
    assert out["second"] == "Tomas Adeyemi"
    assert out["early"] == ["9001", "9002"]


def test_two_answers_are_waited_for_at_most_and_who_is_on_show_is_asked_about_next() -> None:
    out = _run("pass")
    assert out["before"] == ["9001", "9002"]
    assert out["asked"] == ["9001", "9002", "9003"]


def test_an_answer_about_someone_the_pointer_has_left_is_not_shown() -> None:
    out = _run("pass")
    assert (out["late"]["name"], out["late"]["loading"], out["late"]["born"]) == (
        "Iris Lindqvist",
        True,
        "",
    )
    assert out["then"] == "Someone else."


def test_a_person_only_brushed_past_while_a_card_is_open_is_not_asked_about() -> None:
    assert _run("brush") == {"passed": "Tomas Adeyemi", "asked": ["9001"]}


def test_a_failed_card_keeps_who_they_are_and_asks_again_only_when_told_to() -> None:
    out = _run("failed")
    assert (out["failed"]["name"], out["failed"]["role"]) == ("Mara Ellison", "as Part 0")
    assert "Mara" in out["failed"]["failed"]
    assert out["back"] == {"failed": True, "asked": 1}
    assert out["again"] == {"loading": True, "asked": 2}
    assert out["then"] == "Trained in <b>Leeds</b>."
    # The press was for that alone.
    assert out["people"] == 0
    # TMDB has no such person: trying again could only fail again.
    assert out["retryWhenGone"] is False


def test_a_card_says_only_what_is_known() -> None:
    died, unknown, no_age, little = _run("little")
    assert died == "Born 1949, died 2019, aged 70"
    assert unknown == ""
    assert no_age == "Born 1990"
    assert little["bio"] and "No titles" in little["none"]


def test_a_touch_opens_no_card_and_a_press_opens_the_person() -> None:
    out = _run("presses")
    assert out["touch"] is None
    who, _ = out["tapped"][0]
    assert (who["person_id"], who["name"]) == ("9001", "Mara Ellison")
    assert who["profile_url"].endswith("/0.jpg")


def test_a_card_leads_to_a_title_of_theirs_and_to_all_of_them() -> None:
    out = _run("presses")
    ref, _ = out["poster"]["opened"][0]
    # The library's own, where it has it; and not the person's page as well.
    assert ref == "movie-12"
    assert (out["poster"]["card"], out["poster"]["people"]) == (None, 1)
    assert (out["all"][0]["person_id"], out["all"][0]["job"]) == ("9002", "Director")
    assert out["card"] is None


def test_someone_tmdb_gives_no_id_for_is_words_and_nothing_to_press() -> None:
    assert _run("unknownAndKeys")["plain"] == {"tag": "DIV", "tabbable": False, "card": None}


def test_coming_to_a_person_by_the_keyboard_opens_their_card_and_escape_takes_it_first() -> None:
    out = _run("unknownAndKeys")
    assert out["focused"] == "Mara Ellison"
    assert (out["first"], out["card"], out["second"]) == (True, None, False)


def test_a_strip_drawn_again_takes_its_card_away_and_keeps_what_came_meanwhile() -> None:
    out = _run("redrawn")
    assert out["gone"] is None
    assert out["again"] == "Trained in <b>Leeds</b>."
    assert out["asked"] == ["9001"]


def test_people_who_come_under_a_still_pointer_by_a_scroll_open_no_card() -> None:
    assert _run("scrolled") == {"soon": None, "still": None, "then": True}


def test_a_photo_called_home_midway_flies_on_until_it_is_home() -> None:
    assert _run("turnedRound") == [1, 2, 1, True, 0, False]


def test_a_person_the_pointer_left_before_the_next_frame_opens_no_card() -> None:
    assert _run("leftBeforeTheFrame") == {
        "left": None,
        "rested": "Tomas Adeyemi",
        "asked": ["9002"],
    }
