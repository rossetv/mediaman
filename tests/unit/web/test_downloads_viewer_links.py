"""A viewer is never offered a link into Radarr or Sonarr by the downloads API, and nobody is
sent the key a retry is read by."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user, insert_settings


def _item(item_id: str, **extra):
    return {
        "id": item_id,
        "title": "T " + item_id,
        "media_type": "movie",
        "state": "searching",
        "state_label": "Searching",
        "progress": 0,
        "abandon_visible": True,
        "stuck_seasons": [],
        "arr_id": 1,
        "arr_link": "http://radarr.internal:7878/movie/x",
        "arr_source": "Radarr",
        **extra,
    }


def _payload(*_args):
    return {
        "hero": _item("radarr:1"),
        "queue": [_item("sonarr:2", arr_source="Sonarr", media_type="tv")],
        "upcoming": [],
        "recent": [],
        "subtitle": "",
        "unreachable": [],
        "refused": [],
        "outage": "",
        "downloads_set_up": {"movies": True, "shows": True},
    }


def _api(app_factory, conn, role, payload=_payload) -> dict:
    insert_admin_user(conn, username="person", role=role)
    app = app_factory(downloads_router, conn=conn)
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", create_session(conn, "person"))
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=payload):
        return client.get("/api/downloads").json()


def test_a_viewer_is_given_no_link_into_radarr_or_sonarr(app_factory, conn):
    body = _api(app_factory, conn, "viewer")
    assert "radarr.internal" not in str(body)
    assert [body["hero"], *body["queue"]] == [
        {**i, "arr_link": "", "arr_source": ""} for i in (body["hero"], *body["queue"])
    ]


def test_an_admin_is_given_the_link(app_factory, conn):
    body = _api(app_factory, conn, "admin")
    assert body["hero"]["arr_link"] == "http://radarr.internal:7878/movie/x"


def _held(*_args):
    held = {
        "state": "needs_attention",
        "state_label": "Needs attention",
        "attention": "Not an upgrade for existing movie file",
        "attention_hint": "Fix it in Radarr: Activity → Queue",
        "retry_key": "radarr:1",
    }
    return {**_payload(), "hero": _item("radarr:1", **held), "queue": [_item("radarr:3", **held)]}


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_key_a_retry_is_read_by_is_sent_to_nobody(app_factory, conn, role):
    body = _api(app_factory, conn, role, _held)
    assert not [i for i in (body["hero"], *body["queue"]) if "retry_key" in i]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_a_download_mediaman_will_retry_is_told_as_retrying(app_factory, conn, role):
    assert _api(app_factory, conn, role, _held)["hero"]["retrying"] is True


def test_a_download_left_alone_by_the_setting_is_not_told_as_retrying(app_factory, conn):
    insert_settings(conn, import_retry_enabled="false")
    assert _api(app_factory, conn, "admin", _held)["hero"]["retrying"] is False
