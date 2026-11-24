"""account.js run under node against a fake DOM: the Profile page's email form and sign-ins."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "account_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_live_sign_ins_are_listed_and_the_others_can_be_signed_out() -> None:
    out = _run("sessions")
    assert (out["first"]["rows"], out["first"]["disabled"]) == (2, False)
    assert "30 May 2026" in out["first"]["text"] and "1 Jun 2026" in out["first"]["text"]
    assert out["posts"] == 1
    assert (out["after"]["rows"], out["after"]["disabled"]) == (1, True)
    assert out["after"]["say"][0].endswith(" ok")


def test_why_the_others_cannot_be_signed_out_is_said_while_there_are_none() -> None:
    assert _run("onlySession") == {"disabled": True, "why": True}
    out = _run("sessions")
    # not while there are others, nor beside the word that they were just signed out
    assert (out["first"]["why"], out["after"]["why"]) == (False, False)


def test_sign_ins_that_cannot_be_listed_can_still_be_ended_and_a_failure_is_said() -> None:
    out = _run("sessionsFail")
    assert len(out["first"]["rows"]) == 1 and "Could not load" in out["first"]["rows"][0]
    assert out["first"]["disabled"] is False
    assert out["after"] == {
        "focused": "account-revoke-others",
        "disabled": False,
        "say": ["inline-form-msg err", "No luck"],
    }


def test_signing_out_the_others_keeps_the_focus_and_ends_it_on_the_list() -> None:
    out = _run("sessions")
    assert out["whileSending"] == "account-revoke-others"
    assert out["after"]["focused"] == "account-sessions"


def test_a_new_password_is_sent_once_keeps_the_focus_and_reloads_the_sign_ins() -> None:
    out = _run("password")
    assert out["posts"] == 1
    assert out["whileSending"] == out["focused"] == "account-submit-password"
    assert out["say"][0].endswith(" ok")
    assert out["rows"] == 1


def test_a_new_password_on_its_way_is_said_and_typing_meanwhile_leaves_the_words() -> None:
    tone, words = _run("password")["working"]
    assert tone == "inline-form-msg" and words


def test_each_new_word_on_a_new_password_is_brought_into_view_by_the_shortest_way() -> None:
    # on a narrow phone the words wrap under the button, where the bottom bar may cover them
    out = _run("password")
    words = ["account-password-result", "nearest"]
    assert out["revealed"] == [words, words]  # being sent, then updated
    assert out["revealedRefusal"] == [words]
    assert out["revealedEmptied"] == []


def test_a_status_brought_into_view_brings_the_button_beside_it() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_profile.css").read_text()
    rule = css.split(".profile-pg .inline-form-actions .inline-form-msg {")[1].split("}")[0]
    margin = re.search(r"scroll-margin-bottom: (\d+)px", rule)
    # a 44px button centred on one 18px line reaches 13px below it
    assert margin and int(margin.group(1)) >= 13


def test_a_late_answer_is_brought_into_view_only_for_a_reader_still_at_its_status() -> None:
    out = _run("lateAnswer")
    for which in ("email", "password", "sessions", "sessionsRefused"):
        assert {where for where, seen in out[which].items() if seen[0]} == set(out[which])
        assert [where for where, seen in out[which].items() if seen[1]] == ["there", "around"]


def test_a_refusal_that_moves_the_cursor_is_brought_into_view_wherever_the_reader_is() -> None:
    out = _run("lateAnswer")
    assert (out["emailRefused"], out["passwordRefused"]) == ([True, True], [True, True])


def test_the_list_of_sign_ins_takes_the_focus_without_pulling_the_page_to_it() -> None:
    assert _run("focusOnTheList") == {"preventScroll": True}


def test_email_is_sent_once_trimmed_and_each_outcome_is_said() -> None:
    out = _run("email")
    assert [body["email"] for _url, body in out["sent"]] == [
        "vic@example.com",
        "",
        "nope",
        "vic@example.com",
    ]
    button = "account-submit-email"
    # the pressed button keeps the focus, and only a refusal moves it, to the field
    assert out["focus"] == [button, button, button, button, button, "account-email", button, button]
    assert out["saved"][0].endswith(" ok")
    assert out["typed"] == ""
    assert out["removed"][0].endswith(" ok") and "off" in out["removed"][1]
    assert out["refused"] == ["inline-form-msg err", "Not an email address"]
    assert out["cancelled"] == ["inline-form-msg", ""]


def test_the_meter_and_the_match_hint_follow_the_typing_and_clear_on_reset() -> None:
    out = _run("strength")
    plain, weak, ok, strong = (
        "fpc-caption",
        "fpc-caption fpc-caption-weak",
        "fpc-caption fpc-caption-ok",
        "fpc-caption fpc-caption-strong",
    )
    assert [out[k]["tone"] for k in ("weak", "ok", "strong")] == [weak, ok, strong]
    assert out["strong"]["meter"] == "100%"
    assert out["strong"]["match"] == plain
    assert out["differs"]["match"] == weak
    assert out["matches"]["match"] == strong and out["matches"]["says"] == "Passwords match"
    # changing the first field is noticed too
    assert out["changed"]["match"] == weak
    assert out["emptied"]["match"] == plain
    assert out["reset"] == {"meter": "0%", "tone": plain, "match": plain}
