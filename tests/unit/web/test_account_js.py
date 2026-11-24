"""account.js run under node against a fake DOM: the Profile page's email form and sign-ins."""

from __future__ import annotations

import json
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
