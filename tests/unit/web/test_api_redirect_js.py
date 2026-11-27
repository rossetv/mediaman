"""A read the server answers with the sign-in or password-change page leads there, not to an empty page."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "api_redirect_harness.js"
ORIGIN = "https://mm.example"


def _run(pathname="/settings", search="", answer=None, **case) -> dict:
    case.update(location={"pathname": pathname, "search": search}, answer=answer or {})
    result = subprocess.run(
        ["node", str(HARNESS), json.dumps(case)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _redirect(to: str) -> dict:
    return {"status": 200, "redirected": True, "url": ORIGIN + to, "body": None}


def test_a_read_redirected_to_sign_in_leads_there_and_back_to_the_page():
    out = _run(search="?tab=a%26b", answer=_redirect("/login?next=%2Fapi%2Fx"))

    assert out["href"] == "/login?next=" + "%2Fsettings%3Ftab%3Da%2526b"
    assert out["settled"] == "pending"


def test_a_read_redirected_to_the_password_change_leads_there():
    out = _run(answer=_redirect("/force-password-change"))

    assert out["href"] == "/force-password-change"
    assert out["settled"] == "pending"


def test_a_read_answered_401_leads_to_sign_in():
    out = _run(answer={"status": 401, "body": {"detail": "Not authenticated"}})

    assert out["href"] == "/login?next=%2Fsettings"


@pytest.mark.parametrize(
    ("answer", "status", "redirect"),
    [
        (_redirect("/login"), 401, "/login"),
        (_redirect("/force-password-change"), 403, "/force-password-change"),
        ({"status": 401, "body": {}}, 401, None),
    ],
)
def test_unsaved_edits_keep_the_page_and_the_read_fails(answer, status, redirect):
    out = _run(answer=answer, unsaved=True)

    assert out["href"] is None
    assert out["settled"] == "rejected"
    assert out["apiError"] is True
    assert out["status"] == status
    assert out.get("redirect") == redirect
    assert len(out["toasts"]) == 1


@pytest.mark.parametrize("pathname", ["/login", "/force-password-change"])
@pytest.mark.parametrize("to", ["/login", "/force-password-change"])
def test_a_redirect_on_the_page_it_leads_to_does_not_loop(pathname, to):
    out = _run(pathname=pathname, answer=_redirect(to))

    assert out["href"] is None
    assert out["settled"] == "rejected"
    assert out["apiError"] is True


@pytest.mark.parametrize(
    "url",
    [ORIGIN + "/somewhere", "https://evil.example/login", ORIGIN + "/login/extra"],
)
def test_a_redirect_to_any_other_page_is_not_followed(url):
    out = _run(answer={"status": 200, "redirected": True, "url": url, "body": {"items": []}})

    assert out["href"] is None
    assert out["settled"] == "resolved"


def test_a_page_address_that_reads_as_another_host_is_not_kept_for_after_sign_in():
    out = _run(pathname="//evil.example/x", answer=_redirect("/login"))

    assert out["href"] == "/login"


def test_a_caller_that_leaves_by_itself_gets_the_failure_and_nothing_moves():
    out = _run(answer=_redirect("/force-password-change"), options={"stay": True})

    assert out["href"] is None
    assert out["settled"] == "rejected"
    assert out["redirect"] == "/force-password-change"
    assert out["toasts"] == []


def test_a_write_that_ends_on_the_sign_in_page_is_not_sent_there():
    out = _run(answer={**_redirect("/login"), "body": {"ok": True}}, method="post")

    assert out["href"] is None
    assert out["settled"] == "resolved"


@pytest.mark.parametrize(
    ("answer", "href"),
    [
        (_redirect("/login"), "/login?next=%2Fsettings"),
        (_redirect("/force-password-change"), "/force-password-change"),
        ({"status": 401, "body": {}}, "/login?next=%2Fsettings"),
    ],
)
def test_unsaved_edits_ask_first_and_a_yes_leaves(answer, href):
    out = _run(answer=answer, unsaved=True, confirm=True)

    assert len(out["asked"]) == 1
    assert out["href"] == href
    assert out["settled"] == "pending"
    assert out["toasts"] == []


def test_a_no_keeps_the_edits_and_several_reads_ask_once():
    out = _run(answer=_redirect("/login"), unsaved=True, confirm=False, reads=3)

    assert len(out["asked"]) == 1
    assert out["href"] is None
    assert out["all"] == ["rejected"] * 3
    assert out["status"] == 401
    assert len(out["toasts"]) == 1


def test_a_normal_read_resolves_with_its_body():
    out = _run(answer={"status": 200, "body": {"items": [1]}}, unsaved=True, confirm=True)

    assert out["settled"] == "resolved"
    assert out["data"] == {"items": [1]}
    assert out["href"] is None
    assert out["asked"] == []
