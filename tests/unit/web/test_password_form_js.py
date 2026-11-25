"""password-form.js under node against a fake DOM: the forms that take a new password, and the rules they show."""

from __future__ import annotations

import itertools
import json
import random
import subprocess
from functools import cache
from pathlib import Path

import pytest

from mediaman.web.auth.password_policy import MAX_BYTES, password_issues
from mediaman.web.routes.users.passwords import rules_of

HARNESS = Path(__file__).parent / "js" / "password_form_harness.js"
# What each line of the page stands for among the server's rules.
_LINES = {
    "length": {"length", "too_long"},
    "mix": {"mix"},
    "guess": {"repeats", "username", "sequence"},
}


@cache
def _run(scenario: str, given: str = "") -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario],
        input=given,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _made_up() -> list[tuple[str, str]]:
    """Passwords near every edge of the policy, with a username each."""
    rng = random.Random(7)
    kinds = [
        "abcdefghijklmnopqrstuvwxyz",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "0123456789",
        "!?#- _.",
        "qwertyuiop",
        "ab",
        "áéñžßжψ",
        "ЖÉΨİ",
        "٣۴",
        "日本語漢字",
        "\u00a0\u2003\t",
        # that fold to others
        "\ufb01\u2460\uff41\uff21\uff11\u00b2\u00bd\u2167",
        # that are a space to one side alone
        "\x1c\x85\ufeff\u180e\u200b",
        "\U0001f600\U0001f98a",
        # digits and letters of rare scripts, and what one side may be too old to know
        "\u1369\U00010a40\U0001e900\U0001e922\u1c89\u0295\ua7cb\U0001ccd6\U0001ccd7\U00010d50\U00010d70\U00010d40\U0001fae8",
    ]
    made = []
    for _ in range(4000):
        pool = "".join(rng.sample(kinds, rng.randint(1, 4)))
        word = "".join(
            rng.choice(pool) for _ in range(rng.choice([1, 5, 6, 11, 12, 13, 19, 20, 30]))
        )
        user = rng.choice(["", "casey", " Casey ", "ab", word[:4], word + "x", "ÉLAN"])
        made.append((rng.choice([word, word, word + user, word + "12345"]), user))
    runs = ["abcdefghijklmnop", "0987654321", "qwertyuiop", "mlkjihgfedcba"]
    ends = ["", "X1!x", "Q!7zR", "9!Z xQ"]
    made += [
        (run[:n] + end, "casey") for run, n, end in itertools.product(runs, [7, 8, 9, 10], ends)
    ]
    full = "Aa1!xyzw" + "é" * ((MAX_BYTES - 8) // 2)
    return made + [(full + end, "casey") for end in ("", "x", "xy")]


@pytest.fixture(scope="module")
def both() -> list[tuple[str, set[str], dict]]:
    made = _made_up()
    read = _run("read", json.dumps(made))
    return [
        (ascii(password), set(rules_of(password_issues(password, user))), seen)
        for (password, user), seen in zip(made, read, strict=True)
    ]


def test_no_rule_is_shown_as_met_that_the_server_finds_broken(both) -> None:
    for password, broken, seen in both:
        # said alone, whatever else is wrong
        if "too_long" in broken:
            assert not seen["ok"]["length"], password
            continue
        for line, rules in _LINES.items():
            assert not (seen["ok"][line] and rules & broken), (password, line, broken)


def test_a_rule_the_server_finds_kept_is_shown_as_met(both) -> None:
    for password, broken, seen in both:
        if "too_long" in broken:
            continue
        for line, rules in _LINES.items():
            if seen["unsure"].get(line):
                continue
            assert seen["ok"][line] == (not rules & broken), (password, line, broken)


def test_the_made_up_passwords_reach_every_rule_both_ways(both) -> None:
    for rule in set.union(*_LINES.values()):
        kept = sum(rule not in broken for _password, broken, _seen in both)
        assert 0 < kept < len(both), rule
    assert any(seen["unsure"]["mix"] for _password, _broken, seen in both)
    assert any(seen["unsure"]["guess"] for _password, _broken, seen in both)
    assert any(all(seen["ok"].values()) for _password, _broken, seen in both)


def test_each_rule_is_ticked_by_itself_as_it_is_met() -> None:
    out = _run("typing")
    unmet = {"length": "", "mix": "", "guess": "", "same": "", "common": "later"}
    assert out["none"] == unmet
    assert out["good"] == unmet | {"length": "met", "mix": "met", "guess": "met"}
    # not known to be another one until the current one is typed
    assert out["withOld"]["same"] == "met"
    assert out["same"]["same"] == "off"
    assert out["emptied"][0] == unmet


def test_a_guessable_password_is_told_why_where_the_rule_stands() -> None:
    out = _run("typing")
    assert (out["username"][0]["guess"], out["run"][0]["guess"]) == ("off", "off")
    assert "username" in out["username"][1] and "order" in out["run"][1]
    assert out["emptied"][1] == "No username"


def test_a_password_the_server_finds_too_long_is_never_called_done() -> None:
    assert _run("typing")["huge"]["length"] == "off"
    assert "fewer" in _run("said")["huge"][1]


def test_what_is_still_needed_is_said_once_the_typing_pauses() -> None:
    out = _run("said")
    assert out["short"][0] == ""
    assert "6 more characters" in out["short"][1]
    assert out["good"][1].startswith("Nothing left to add")
    assert out["emptied"] == ["", ""]


def test_an_eye_shows_its_own_field_and_keeps_the_cursor_where_it_was() -> None:
    out = _run("eye")
    assert out["first"] == ["password", "password", ["false", "false"]]
    assert out["shown"] == ["password", "text", ["false", "true"]]
    assert out["kept"] == ["new_password", 2, 4]


def test_a_shown_password_is_hidden_while_it_is_sent_and_shown_again_if_refused() -> None:
    out = _run("eye")
    assert out["sending"][:2] == ["password", "password"]
    assert out["refused"] == out["shown"]
    assert out["reset"] == out["first"]


def test_caps_lock_is_said_under_the_field_in_use_and_never_over_a_reason() -> None:
    out = _run("caps")
    assert out["on"] == ["Caps Lock is on", ""]
    assert out["moved"] == ["", "Caps Lock is on"]
    assert "current password" in out["wrong"][0]
    assert out["off"] == [out["wrong"][0], ""]


@pytest.mark.parametrize(
    ("typed", "at"),
    [
        ("empty", 0),
        ("noNew", 1),
        ("short", 1),
        ("mix", 1),
        ("username", 1),
        ("same", 1),
        ("huge", 1),
    ],
)
def test_what_the_rules_refuse_is_not_sent_and_said_under_its_field(typed, at) -> None:
    out = _run("refusals")[typed]
    field = ("old_password", "new_password")[at]
    assert (out["sent"], bool(out["says"][at]), out["says"][1 - at]) == (0, True, "")
    assert out["focused"] == field
    assert out["shown"] == [field, field + "-why"]
    assert (out["invalid"][at], out["invalid"][1 - at]) == ("true", None)
    assert out["typed"] == ""


def test_a_password_that_keeps_the_rules_is_sent_and_one_only_the_server_can_judge_too() -> None:
    out = _run("refusals")
    assert (out["good"]["sent"], out["good"]["says"]) == (1, ["", ""])
    assert out["unsure"]["sent"] == 1
    assert out["newer"]["sent"] == 1


def test_the_rules_the_server_names_are_marked_until_the_password_is_another_one() -> None:
    out = _run("server")
    assert (out["common"]["says"], out["common"]["rules"]["common"]) == (
        "On the common list.",
        "bad",
    )
    assert (out["many"]["rules"]["guess"], out["many"]["rules"]["common"]) == ("bad", "bad")
    assert out["same"]["rules"]["same"] == "bad"
    for name, seen in out.items():
        assert seen["focused"] == "new_password", name
        assert "bad" not in seen["typed"].values(), name
        assert seen["typedSays"] == "", name
        # nor once it is someone else's
        assert "bad" not in seen["other"].values(), name


def test_a_refusal_no_line_can_show_is_listed_under_the_field_and_one_that_can_is_not() -> None:
    out = _run("server")
    assert out["many"]["items"] == []
    assert out["unnamed"]["items"] == ["Something new."]
    assert out["bare"]["items"] == ["Something new.", "And more."]
    # longer than its room: its rule says it
    assert (out["long"]["says"], out["long"]["items"]) == ("It does not pass.", [])


def test_what_is_wrong_with_the_whole_goes_once_the_person_types() -> None:
    assert _run("whole") == {"told": ["Too many.", True], "typed": ["", False]}


def test_a_form_is_sent_once_and_is_free_again_after_a_refusal() -> None:
    out = _run("busy")
    assert len(out["sent"]) == 1
    assert (out["focused"], out["disabled"], out["readOnly"]) == ("go", "true", True)
    assert (out["busy"], out["says"]) == (True, "Saving…")
    assert out["after"] == {"busy": False, "says": "Save", "readOnly": False}


def test_the_forced_page_posts_once_with_the_new_password_in_both_fields() -> None:
    good = _run("forced")["good"]
    assert (good["sent"], good["second"], good["busy"], good["guarded"]) == (
        True,
        False,
        True,
        True,
    )
    assert good["again"] == "Copper lantern 41"
    # one new password for a password manager to see
    assert good["off"] is True
    assert "pwd-once" in good["hides"].split()


@pytest.mark.parametrize("typed", ["good", "none", "weak", "same"])
def test_no_password_is_kept_in_the_second_field_or_in_an_attribute(typed) -> None:
    out = _run("forced")[typed]
    assert (out["kept"], out["attributes"], out["type"]) == ("", False, "password")


@pytest.mark.parametrize(
    ("typed", "field"),
    [("none", "old_password"), ("weak", "new_password"), ("same", "new_password")],
)
def test_the_forced_page_keeps_what_was_typed_when_it_would_be_refused(typed, field) -> None:
    # sending it would bring the page back with every field emptied
    out = _run("forced")[typed]
    assert (out["sent"], out["focused"]) == (False, field)
    # and the form is free for the next try at once
    assert (out["busy"], out["guarded"]) == (False, False)


def test_the_forced_page_can_be_sent_again_when_no_answer_came_or_on_the_way_back() -> None:
    out = _run("forced")
    assert out["lapsed"] == [False, False, True]
    assert out["back"] is False


def test_the_forced_page_shows_what_the_server_marked_until_the_person_types() -> None:
    out = _run("refusedPage")
    marked, words = out["first"]
    assert (marked["guess"], marked["common"]) == ("bad", "bad")
    assert "username" in words
    assert "bad" not in out["typed"].values()


def test_the_tick_takes_the_place_of_the_form_and_gives_it_back() -> None:
    out = _run("celebrate")
    # what was typed is text
    assert out["during"] == [True, "Password changed<b>words"]
    assert out["ticked"] is True
    assert out["after"] == [False, False, False]
