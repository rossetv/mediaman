"""The Plex account card's script, run under node over the panels of its template."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).parent / "js" / "profile_plex_harness.js"


def _run(scenario: str) -> Any:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_linking_goes_from_open_plex_to_is_this_you_to_linked() -> None:
    out = _run("link")
    assert out["first"] == {"shown": "unlinked", "focus": "", "status": ""}
    assert out["waiting"]["shown"] == "waiting"
    assert out["waiting"]["focus"] == "plex-open"
    assert out["waiting"]["href"] == "https://app.plex.tv/auth#?code=x"
    assert out["waiting"]["app"] == "mediaman (h)"
    assert out["still"] == "waiting"
    assert out["confirm"]["shown"] == "confirm"
    assert out["confirm"]["focus"] == "plex-confirm"
    assert out["confirm"]["status"] != out["waiting"]["status"]
    assert out["confirm"]["name"] == "<b>amy</b>"
    assert out["confirm"]["waits"] == []
    assert out["sent"] == ['POST /api/account/plex {"attempt":"a1"}']
    assert out["linked"]["shown"] == "linked"
    assert "Reading" in out["linked"]["read"]
    assert "last read" in out["read"]["text"]
    assert out["read"]["waits"] == []


def test_only_plexs_own_approval_page_is_opened() -> None:
    out = _run("badUrl")
    assert (out["shown"], out["href"]) == ("failed", None)


def test_an_answer_after_cancelling_changes_nothing() -> None:
    out = _run("cancelWhileChecking")
    assert (out["shown"], out["waits"], out["cancelled"]) == ("unlinked", [], True)


def test_a_hidden_tab_does_not_ask_and_asks_at_once_on_return() -> None:
    out = _run("hidden")
    assert (out["whileHidden"], out["after"]) == (0, 1)
    assert out["waits"] == [2000]


def test_a_failed_sign_in_says_why_and_whether_plex_signed_out() -> None:
    refused, ran_out = _run("failures")
    assert (refused["shown"], refused["text"]) == ("failed", "No access here.")
    assert refused["status"] == "No access here."
    assert refused["note"] is True
    assert (ran_out["shown"], ran_out["note"]) == ("failed", False)
    assert "ran out" in ran_out["text"]
    assert ran_out["focus"] == "plex-retry"


def test_a_failing_check_waits_longer_and_does_not_end_the_sign_in() -> None:
    out = _run("slowPlex")
    assert (out["shown"], out["waits"]) == ("waiting", [10000])
    assert "slow" in out["wait"]


def test_unlinking_asks_first() -> None:
    out = _run("unlink")
    assert out["kept"]["shown"] == "linked"
    assert out["after"] == "unlinked"
    assert out["last"] == "DELETE /api/account/plex"


def test_an_admin_unlinks_another_person_by_a_named_button() -> None:
    out = _run("others")
    assert out["name"] == "<i>bob</i>"
    assert "<i>bob</i>" in out["label"]
    assert out["deleted"] is True
    assert "Nobody else" in out["left"]
