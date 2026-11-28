"""A viewer is never offered a link into Radarr or Sonarr: not by the downloads API, nor by the
old Downloads page template drawn from what the API says (the address now leads to Watch)."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user, insert_settings

_TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(mediaman.__file__).parent / "web" / "templates")),
    autoescape=True,
)


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


def _old_page(body: dict, role: str) -> str:
    """The old Downloads page template, drawn from an answer of the API."""
    scope = {"type": "http", "path": "/", "query_string": b"", "headers": [], "state": {}}
    told = ("hero", "queue", "upcoming", "recent", "subtitle", "outage", "refused")
    return _TEMPLATES.get_template("downloads.html").render(
        request=Request(scope),
        nav_active="downloads",
        downloads_set_up=body["downloads_set_up"],
        strip=body["strip"],
        can_abandon=role == "admin",
        **{key: body[key] for key in told},
    )


def _page_and_api(app_factory, conn, role, payload=_payload):
    insert_admin_user(conn, username="person", role=role)
    app = app_factory(downloads_router, conn=conn)
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", create_session(conn, "person"))
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=payload):
        body = client.get("/api/downloads").json()
    return _old_page(body, role), body


def _links(html: str) -> list[str]:
    return re.findall(r"<a [^>]*data-v=\"arr-link\"[^>]*>", html)


def test_a_viewer_is_given_no_link_into_radarr_or_sonarr(app_factory, conn):
    html, body = _page_and_api(app_factory, conn, "viewer")
    assert not _links(html) and "View in" not in html
    assert "radarr.internal" not in html
    assert [body["hero"], *body["queue"]] == [
        {**i, "arr_link": "", "arr_source": ""} for i in (body["hero"], *body["queue"])
    ]


def test_an_admin_is_given_the_link_by_the_old_page_template(app_factory, conn):
    html, _ = _page_and_api(app_factory, conn, "admin")
    assert any("href=" in tag for tag in _links(html))


def _heading(app_factory, conn, states):
    def payload(*_args):
        queue = [_item(f"sonarr:{n}", state=state) for n, state in enumerate(states)]
        return {**_payload(), "queue": queue}

    return _page_and_api(app_factory, conn, "admin", payload)[0]


def test_on_the_old_page_template_rows_that_all_wait_sit_under_up_next(app_factory, conn):
    assert "Up next</h2>" in _heading(app_factory, conn, ["queued", "queued"])


def test_on_the_old_page_template_rows_that_download_sit_under_also_in_progress(app_factory, conn):
    assert "Also in progress</h2>" in _heading(app_factory, conn, ["queued", "downloading"])


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
    _, body = _page_and_api(app_factory, conn, role, _held)
    assert not [i for i in (body["hero"], *body["queue"]) if "retry_key" in i]


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_a_download_mediaman_will_retry_says_so_and_not_where_to_fix_it(app_factory, conn, role):
    html, body = _page_and_api(app_factory, conn, role, _held)
    assert body["hero"]["retrying"] is True
    assert html.count("Fixing — finding another copy") == 2
    assert "Fix it in Radarr" not in html


def test_a_download_left_alone_by_the_setting_says_where_to_fix_it(app_factory, conn):
    insert_settings(conn, import_retry_enabled="false")
    html, body = _page_and_api(app_factory, conn, "admin", _held)
    assert body["hero"]["retrying"] is False
    assert "Fix it in Radarr" in html and "another copy" not in html


def test_a_viewer_is_told_a_download_that_needs_attention_waits_for_an_admin(app_factory, conn):
    insert_settings(conn, import_retry_enabled="false")
    html, _ = _page_and_api(app_factory, conn, "viewer", _held)
    assert html.count("Won’t finish until an admin sorts it out") == 2
    assert "Fix it in Radarr" not in html
