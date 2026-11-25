"""Where the form that adds a user, or a user's row, says why it refused, run under node against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "users_add_harness.js"


@pytest.fixture(scope="module")
def outcomes() -> dict:
    result = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def refusals(outcomes) -> dict:
    return outcomes["add"]


@pytest.mark.parametrize(
    ("refusal", "field", "words"),
    [
        ("shortName", "new-username", "3 characters"),
        ("takenName", "new-username", "taken"),
        ("shortPassword", "new-user-password", "7 more characters"),
        ("usernameInIt", "new-user-password", "their username"),
        ("weakPassword", "new-user-password", "Add a digit"),
        ("unnamedRule", "new-user-password", "NoAdd a digitAnd a hat"),
    ],
)
def test_a_refused_field_has_the_cursor_and_its_reason_beside_it(
    refusals, refusal, field, words
) -> None:
    out = refusals[refusal]
    assert out["said"] == [field + "-why"]
    assert words in out["words"]
    assert out["focused"] == field
    assert out["last"] == [field, field + "-why"]


@pytest.mark.parametrize("refusal", ["limit", "offline", "serverError"])
def test_a_refusal_no_field_can_put_right_is_said_at_the_button_pressed(refusals, refusal) -> None:
    out = refusals[refusal]
    assert out["said"] == ["create-user-result"]
    assert out["focused"] == "btn-submit-add-user"
    assert out["last"][-1] == "create-user-result"


def test_such_a_refusal_leaves_a_person_who_moved_on_where_they_are(refusals) -> None:
    assert refusals["limitElsewhere"]["focused"] == "elsewhere"


def test_a_reason_goes_once_the_person_types(refusals) -> None:
    assert [out["typed"] for out in refusals.values()] == [[]] * len(refusals)


@pytest.mark.parametrize("refusal", ["takenElsewhere", "takenScrolled"])
def test_a_refusal_that_comes_once_the_reader_has_moved_on_only_says_why(refusals, refusal) -> None:
    out = refusals[refusal]
    assert out["said"] == ["new-username-why"]
    assert out["focused"] != "new-username"
    assert out["late"] == []


def test_what_the_page_itself_finds_wrong_is_not_sent(refusals) -> None:
    unsent = {"shortName", "shortPassword", "usernameInIt"}
    assert {refusal for refusal, out in refusals.items() if not out["sends"]} == unsent


def test_a_refused_password_is_kept_and_the_rule_it_broke_is_marked(refusals) -> None:
    assert refusals["weakPassword"]["red"] == ["common"]
    assert all(out["kept"] for out in refusals.values())


def test_a_user_made_is_drawn_and_said_before_the_form_folds_away_empty(outcomes) -> None:
    out = outcomes["made"]
    (url, body), during, after = out["posts"][0], out["during"], out["after"]
    assert (len(out["posts"]), url) == (1, "/api/users")
    assert body == {"username": "<b>vic", "password": "Copper lantern 41", "role": "viewer"}
    # what was typed is text
    assert during["drawn"].startswith("<b>vic changes this password")
    assert during["live"].endswith(during["drawn"]) and during["open"]
    assert after == {
        "open": False,
        "drawn": False,
        "left": ["", ""],
        "rows": 1,
        "focused": "btn-add-user",
        "busy": "false",
    }


def test_escape_folds_the_form_away_empty(outcomes) -> None:
    assert outcomes["escape"] == {"open": False, "left": "", "focused": "btn-add-user"}


def test_the_password_dialog_cancelled_leaves_the_focus_on_the_button_pressed(outcomes) -> None:
    assert outcomes["cancel"] == {"focused": "btn-submit-add-user", "said": ""}


def test_a_form_that_is_sending_is_sent_once_and_cannot_be_closed(outcomes) -> None:
    out = outcomes["sending"]
    assert (out["posts"], out["open"], out["focused"]) == (1, True, "btn-submit-add-user")
    assert out["said"]


@pytest.mark.parametrize(
    ("action", "there"), [("role", "role"), ("delete", "delete"), ("email", "field")]
)
def test_a_refused_change_of_a_user_moves_nothing_for_a_reader_who_has_moved_on(
    outcomes, action, there
) -> None:
    out = outcomes["rows"][action]
    assert {where: seen["focused"] for where, seen in out.items()} == {
        "there": there,
        "elsewhere": "elsewhere",
        "scrolled": "body",
    }
    assert (out["elsewhere"]["moved"], out["scrolled"]["moved"]) == (False, False)
    assert [seen["said"] for seen in out.values()] == ["No"] * 3
