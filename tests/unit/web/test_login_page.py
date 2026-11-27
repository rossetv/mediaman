"""The sign-in page: what the server tells it to draw, and login.js run under node against a fake DOM."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.web.auth.password_hash import create_user
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router

_WEB = Path(mediaman.web.__file__).parent
HARNESS = Path(__file__).parent / "js" / "login_harness.js"
_SIGNED_OUT = "Signed out. See you soon."


@pytest.fixture
def client(app_factory, conn):
    _limiter.reset()
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    app = app_factory(auth_router, conn=conn, state_extras={"templates": templates})
    create_user(conn, "admin", "correct horse battery 1", enforce_policy=False)
    return TestClient(app)


def _run(scenario: str, **opts) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario, json.dumps(opts)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _message(html: str) -> str:
    """The class of the line that says why a sign-in was refused, the offline one aside."""
    found = re.findall(r'<div class="(login-msg[^"]*)" role="alert">', html)
    assert len(found) == 1, found
    return found[0]


def test_the_page_says_signed_out_only_after_a_log_out(client):
    after = client.get("/login?out=1").text
    assert _SIGNED_OUT in after and "is-out" in after
    # the live region comes empty, for login.js to fill once it is up
    region = re.search(r'<span class="sub-out" role="status"[^>]*>(.*?)</span>', after, re.S)[1]
    assert _SIGNED_OUT not in region
    for plain in (client.get("/login").text, client.get("/login?out=0").text):
        assert _SIGNED_OUT not in plain and "is-out" not in plain


def test_a_rate_limited_sign_in_is_drawn_as_a_wait_and_not_as_a_wrong_password(client):
    for name in ("a", "b", "c", "d", "e"):
        wrong = client.post("/login", data={"username": name, "password": "wrong"})
    limited = client.post("/login", data={"username": "admin", "password": "wrong"})

    assert limited.status_code == 429
    assert _message(limited.text) == "login-msg is-wait"
    assert "is-bad" not in limited.text
    # a wrong password marks the field and is not a wait
    assert _message(wrong.text) == "login-msg"
    assert '<div class="login-box is-bad">' in wrong.text


def test_the_page_runs_login_js_at_its_end_and_not_deferred(client):
    html = client.get("/login").text
    tag = re.search(r'<script src="/static/js/login\.js[^"]*"[^>]*>', html)
    assert tag and "defer" not in tag[0] and "async" not in tag[0]
    assert html.index(tag[0]) > html.index('id="login-down"')


def test_a_stale_arrival_flag_is_removed_as_the_page_loads():
    out = _run("load", stored={"mm.arrive": '{"t":1,"sky":[]}', "other": "kept"})
    assert out["stored"] == {"other": "kept"}


def test_signing_in_leaves_watch_the_time_and_where_each_light_was():
    out = _run("submit")
    assert out["sent"] == {"posted": True, "busy": True}
    assert out["flag"]["sky"] == ["matrix(1.02, 0, 0, 1.02, 12, 8)", "none"]
    assert isinstance(out["flag"]["t"], int) and out["flag"]["t"] > 0


@pytest.mark.parametrize(
    ("href", "flag"),
    [
        ("https://mm.test/login?next=%2F%3Fq%3Dx", True),
        ("https://mm.test/login?next=%2Fsettings", False),
    ],
)
def test_only_a_sign_in_that_lands_on_watch_leaves_the_flag(href, flag):
    out = _run("submit", href=href)
    assert out["sent"]["posted"] is True
    assert (out["flag"] is not None) is flag


def test_offline_the_form_does_not_post_and_says_so_in_place_of_the_refusal():
    out = _run("submit", offline=True, refused="Invalid username or password.")
    # not sent, so the password stays and the submit guard never marks the form busy
    assert out["sent"] == {"posted": False, "busy": False}
    assert out["flag"] is None
    assert out["down"] is False and out["refusedShown"] is False and out["bad"] is False
    # one line before and one after: the card stays where it is
    assert out["room"] == {"on": True, "dc": "0px", "df": "0px"}


def test_a_refusal_makes_room_for_its_line_from_where_the_page_was():
    out = _run("load", refused="Invalid username or password.")
    # without the line the card sat 25px lower and the form 50px nearer the card's top
    assert out["room"] == {"on": True, "dc": "25px", "df": "-50px"}
    assert _run("load")["room"]["on"] is False


def test_the_signed_out_mark_leaves_the_address_and_the_rest_stays():
    out = _run("load", href="https://mm.test/login?out=1&next=%2Fkeep#here")
    assert out["replaced"] == ["/login?next=%2Fkeep#here"]
    assert _run("load", href="https://mm.test/login?next=%2F")["replaced"] == []


def test_the_signed_out_line_is_put_in_its_live_region_once_the_page_is_up():
    assert _run("load", out=True)["out"] == ["", _SIGNED_OUT]


def test_offline_tapped_again_the_line_is_said_again_and_the_password_has_the_focus():
    out = _run("offlineTwice", offline=True)
    assert out["again"] is True and out["sameBeforeFrame"] is True
    assert out["focused"] == "password"


def test_a_sign_in_unanswered_after_a_while_says_it_is_opening_the_library():
    out = _run("hold")
    assert out["atOnce"] == {"hold": False, "said": ""}
    assert out["late"] == {"hold": True, "said": "Opening your library\u2026"}
    # a post no longer under way says nothing more
    assert _run("hold", released=True)["late"] == {"hold": False, "said": ""}
