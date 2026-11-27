"""/downloads opens Downloading on Watch: the old address answers 303 to /?dl=1 for every role
once signed in, and what a viewer may not see stays out of the API and of Watch."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import mediaman
from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.services.downloads.strip import StripItem
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session

_TEMPLATES = Path(mediaman.__file__).parent / "web" / "templates"

_STUCK = {
    "id": "radarr:Stuck Movie#603",
    "title": "Stuck Movie",
    "media_type": "movie",
    "kind": "movie",
    "state": "searching",
    "state_label": "Searching",
    "progress": 0,
    "abandon_visible": True,
    "stuck_seasons": [],
    "arr_id": 41,
    "arr_link": "http://radarr.internal:7878/movie/stuck-movie",
    "arr_source": "Radarr",
    "attention": "No release matched the quality profile",
    "attention_hint": "Check the quality profile in Radarr",
    "tmdb_id": 603,
    "queued_since": "2020-02-02T10:00:00+00:00",
}

# What only NZBGet holds is known by its release's name, which only an admin is told.
_NZB_ONLY = {
    "id": "Other.Film.2021.1080p.WEB-DL-GRP",
    "title": "Other Film",
    "media_type": "movie",
    "kind": "movie",
    "state": "downloading",
    "progress": 40,
}


def _payload(*_args, **_kwargs):
    return {
        "hero": None,
        "queue": [dict(_STUCK), dict(_NZB_ONLY)],
        "upcoming": [],
        "recent": [],
        "subtitle": "1 in progress",
        "unreachable": [],
        "refused": [],
        "outage": "",
        "downloads_set_up": {"movies": True, "shows": True},
    }


@pytest.fixture
def signed_in(conn, monkeypatch, secret_key):
    """A client of the real app signed in as a person of the given role."""
    monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)

    def _make(role: str, *, must_change: bool = False) -> TestClient:
        create_user(
            conn,
            "person",
            "Correct-Horse-9-battery",
            enforce_policy=False,
            role=role,
            must_change_password=must_change,
        )
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, "person"))
        return client

    return _make


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_downloads_address_sends_every_role_to_downloading_on_watch(signed_in, role):
    resp = signed_in(role).get("/downloads", follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/?dl=1")


def test_the_downloads_address_signed_out_goes_to_sign_in_and_back(conn):
    set_connection(conn)

    resp = TestClient(create_app()).get("/downloads", follow_redirects=False)

    assert resp.headers["location"] == "/login?next=%2Fdownloads"


def test_a_person_who_must_change_their_password_is_sent_there_before_downloading(signed_in):
    resp = signed_in("admin", must_change=True).get("/downloads", follow_redirects=False)

    assert resp.headers["location"] == "/force-password-change"


@pytest.mark.parametrize("path", ["/downloads", "/api/downloads"])
def test_the_viewer_guard_still_lets_a_viewer_reach_downloads(guarded_app, conn, path):
    app, reached = guarded_app
    create_user(conn, "v", "password1234", enforce_policy=False, role="viewer")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "v"))

    client.get(path, follow_redirects=False)

    assert reached == [("GET", path, "viewer")]


def test_a_viewer_is_told_nothing_an_admin_alone_uses_by_the_downloads_api(signed_in):
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        page = signed_in("viewer").get("/api/downloads").json()

    item = page["queue"][0]
    assert item["title"] == "Stuck Movie"
    assert not item.get("attention") and not item.get("attention_hint")
    assert not item.get("arr_link") and not item.get("arr_id")
    assert not item.get("abandon_visible")
    assert "Other Film" in str(page)
    assert "WEB-DL" not in str(page) and "radarr.internal" not in str(page)


def test_an_admin_is_told_what_to_act_on_by_the_downloads_api(signed_in):
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        item = signed_in("admin").get("/api/downloads").json()["queue"][0]

    assert item["arr_link"] and item["arr_id"] == 41 and item["abandon_visible"]
    assert item["attention"]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_watch_carries_the_abandon_dialog_and_its_script_for_an_admin_alone(signed_in, role):
    html = signed_in(role).get("/").text

    assert ('id="abandon-modal"' in html) is (role == "admin")
    assert ("/static/js/dl-abandon.js" in html) is (role == "admin")


def test_an_admins_watch_page_keeps_each_id_once(signed_in):
    html = signed_in("admin").get("/").text

    ids = re.findall(r'\sid="([^"]+)"', html)
    assert [i for i in set(ids) if ids.count(i) > 1] == []


def test_a_strip_card_opens_downloading_on_watch_unless_it_knows_its_title():
    assert (
        StripItem(key="radarr:1", title="Film", kind="movie", state="downloading").href == "/?dl=1"
    )


def test_the_noscript_link_opens_downloading_on_watch():
    nav = (_TEMPLATES / "_nav.html").read_text()

    assert '<noscript><a class="nav-noscript" href="/?dl=1">' in nav


@pytest.mark.parametrize("path", ["/", "/downloads"])
def test_the_content_security_policy_is_the_same_on_watch_and_the_old_address(conn, path):
    set_connection(conn)
    csp = (
        TestClient(create_app())
        .get(path, follow_redirects=False)
        .headers["Content-Security-Policy"]
    )

    assert re.sub(r"'nonce-[^']+'", "N", csp) == (
        "script-src 'self' N; style-src 'self' N; style-src-attr 'unsafe-inline'; "
        "default-src 'self'; img-src 'self' data: blob: https://i.ytimg.com "
        "https://www.gravatar.com; connect-src 'self'; media-src 'self' blob:; "
        "frame-src https://www.youtube-nocookie.com; frame-ancestors 'none'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'"
    )


def test_settings_sends_an_admin_to_abandon_under_downloading_on_watch():
    cleanup = (_TEMPLATES / "settings" / "_sec_cleanup.html").read_text()

    assert "Abandon button under Downloading on Watch" in cleanup
    assert "Downloads page" not in cleanup
