"""Each abandon and stop-tracking button is named by its title, not by its label alone."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router
from tests.helpers.factories import insert_admin_user

_WEB = Path(mediaman.__file__).parent / "web"


def _item(item_id: str, title: str) -> dict:
    return {
        "id": item_id,
        "title": title,
        "media_type": "movie",
        "state": "searching",
        "state_label": "Searching",
        "progress": 0,
        "abandon_visible": True,
        "stuck_seasons": [],
        "arr_id": 1,
        "arr_link": "",
        "arr_source": "",
    }


def _payload(*_args):
    return {
        "hero": _item("radarr:1", "Hero Film"),
        "queue": [_item("radarr:2", "Queue Film")],
        "upcoming": [_item("radarr:3", "Soon Film"), _item("radarr:4", "Later Film")],
        "recent": [],
        "subtitle": "",
        "unreachable": [],
        "refused": [],
        "outage": "",
        "downloads_set_up": {"movies": True, "shows": True},
    }


def test_every_abandon_button_is_named_by_its_title(app_factory, conn) -> None:
    insert_admin_user(conn, username="person", role="admin")
    app = app_factory(downloads_router, conn=conn)
    app.state.templates = Jinja2Templates(directory=str(_WEB / "templates"))
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "person"))
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        html = client.get("/downloads").text
    names = re.findall(r'<button[^>]*data-abandon-trigger[^>]*aria-label="([^"]*)"', html)
    assert sorted(names) == [
        "Abandon search: Hero Film",
        "Abandon search: Queue Film",
        "Stop tracking: Later Film",
        "Stop tracking: Soon Film",
    ]
    # the tooltip keeps the plain label
    assert 'title="Stop tracking"' in html and 'title="Abandon search"' in html
