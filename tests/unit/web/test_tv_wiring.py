"""Tests for paired TVs in the whole app: what its TV reads, past the middlewares, and what ends it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.web.auth.password_hash import set_must_change_password
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.auth.session_store import create_session, destroy_session
from mediaman.web.repository import tv as repo
from tests.unit.web.test_cast import ORIGIN
from tests.unit.web.test_tv import (  # noqa: F401
    Tv,
    _lives,
    _play,
    _stream_of,
    app,
    clock,
    lookups,
    manager,
    plex,
)


@pytest.fixture
def whole(app, conn, secret_key, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "strict")
    made = create_app()
    made.state.config = Config(secret_key=secret_key)
    made.state.db = conn
    set_connection(conn)
    return made


@pytest.fixture
def amy(whole, conn):
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('amy', 'x', '2026-01-01', 'viewer')"
    )
    conn.commit()
    phone = TestClient(whole, headers={"User-Agent": "a phone", **ORIGIN})
    session = create_session(conn, "amy", user_agent="a phone", client_ip="testclient")
    phone.cookies.set("session_token", session)
    return phone


@pytest.fixture
def tv(whole):
    return Tv(whole)


@pytest.fixture
def home(tv, amy):
    return tv.pair(amy)


def _segment(url: str) -> str:
    return url.replace("index.m3u8", "seg/00000.ts")


def _watching(tv, amy, home) -> str:
    assert _play(amy, home).status_code == 200
    url = tv.poll()["play"]["url"]
    assert tv.client.get(url).status_code == 200
    assert tv.client.get(_segment(url)).content == b"video bytes"
    return url


def test_a_viewers_tv_plays_after_the_phone_signed_out(tv, amy, home, conn, manager):  # noqa: F811
    url = _watching(tv, amy, home)
    destroy_session(conn, amy.cookies["session_token"])
    manager.reap()
    assert tv.client.get(url).status_code == 200
    assert tv.client.get(_segment(url)).status_code == 200


def _unpair(tv, amy, home, conn):
    assert amy.delete(f"/api/tvs/{home}").status_code == 200


def _row_gone(tv, amy, home, conn):
    conn.execute("DELETE FROM tvs")
    conn.commit()


def _user_gone(tv, amy, home, conn):
    conn.execute("DELETE FROM admin_users WHERE username = 'amy'")
    conn.commit()


def _must_change(tv, amy, home, conn):
    set_must_change_password(conn, "amy", True)
    set_must_change_password(conn, "amy", False)


def _everywhere(tv, amy, home, conn):
    grant_recent_reauth(conn, amy.cookies["session_token"], "amy")
    assert amy.post("/api/users/sessions/revoke-others").status_code == 200
    assert repo.find(conn, home) is not None


@pytest.mark.parametrize("end", [_unpair, _row_gone, _user_gone, _must_change, _everywhere])
def test_the_ticket_and_the_stream_end(tv, amy, home, conn, manager, end):  # noqa: F811
    url = _watching(tv, amy, home)
    stream_id, _ = _stream_of(tv.token)
    end(tv, amy, home, conn)
    assert tv.client.get(url).status_code == 404
    assert tv.client.get(_segment(url)).status_code == 404
    assert not _lives(manager, stream_id)


@pytest.mark.parametrize("origin", [None, "null", "https://elsewhere.example"])
def test_a_tv_with_a_session_cookie_and_any_origin_ends_no_session(tv, amy, home, conn, origin):
    tv.client.cookies.set("session_token", amy.cookies["session_token"])
    tv.client.headers["User-Agent"] = "a TV"
    if origin is not None:
        tv.client.headers["Origin"] = origin
    assert tv.poll()["state"] == "ready"
    assert len(tv.code()[0]) == 6
    (held,) = conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()
    assert held == 1


def test_a_tv_whose_cookie_is_of_someone_who_must_change_their_password_goes_on(
    tv, amy, home, conn
):
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, must_change_password)"
        " VALUES ('bob', 'x', '2026-01-01', 1)"
    )
    conn.commit()
    tv.client.cookies.set("session_token", create_session(conn, "bob"))
    assert tv.poll()["state"] == "ready"


@pytest.mark.parametrize(
    "path",
    [
        "/api/tvs/pair",
        "/api/tvs/1/play",
        "/api/tvs/1/commands",
        "/api/tv/device/poll/",
        "/api/tv/device/poll/x",
        "/api/tv/device/art/x",
        "/api/tv/device/../../tvs/pair",
    ],
)
def test_no_other_address_takes_a_post_of_another_origin(tv, path):
    resp = tv.client.post(path, json={}, headers={"Origin": "https://elsewhere.example"})
    assert resp.status_code == 403


def test_a_page_of_another_origin_is_not_let_send_the_header(tv):
    resp = tv.client.options(
        "/api/tv/device/poll",
        headers={
            "Origin": "https://elsewhere.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-tv-token",
        },
    )
    assert not any(name.startswith("access-control") for name in resp.headers)
    assert resp.status_code >= 400


@pytest.mark.parametrize("path", ["/account", "/pair"])
def test_a_page_of_someone_signed_in_holds_the_remote_once(amy, path):
    page = amy.get(path, follow_redirects=False).text
    assert page.count('id="tv-mini"') == page.count('id="tv-remote"') == 1
    assert "/static/js/tv-remote.js" in page and "/static/css/_tv_remote.css" in page


@pytest.mark.parametrize("path", ["/login", "/tv"])
def test_a_page_of_nobody_holds_no_remote(whole, path):
    page = TestClient(whole).get(path)
    assert page.status_code == 200
    assert 'id="tv-remote"' not in page.text


@pytest.mark.parametrize("flagged", [False, True])
def test_the_tvs_own_page_opens_with_any_cookie_and_ends_no_session(tv, amy, home, conn, flagged):
    set_must_change_password(conn, "amy", flagged)
    session = create_session(conn, "amy", user_agent="a phone", client_ip="testclient")
    tv.client.cookies.set("session_token", session)
    tv.client.headers["User-Agent"] = "a TV"
    (before,) = conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()
    assert tv.client.get("/tv", follow_redirects=False).status_code == 200
    (after,) = conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()
    assert after == before
