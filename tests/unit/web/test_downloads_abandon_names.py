"""Each abandon and stop-tracking button is named by its title, not by its label alone: on the
old Downloads page template, drawn from what the downloads API tells an admin."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

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


def test_on_the_old_page_template_every_abandon_button_is_named_by_its_title(
    app_factory, conn
) -> None:
    insert_admin_user(conn, username="person", role="admin")
    client = TestClient(app_factory(downloads_router, conn=conn))
    client.cookies.set("session_token", create_session(conn, "person"))
    with patch("mediaman.web.routes.downloads.build_downloads_response", side_effect=_payload):
        body = client.get("/api/downloads").json()
    scope = {"type": "http", "path": "/", "query_string": b"", "headers": [], "state": {}}
    told = ("hero", "queue", "upcoming", "recent", "subtitle", "outage", "refused")
    env = Environment(loader=FileSystemLoader(str(_WEB / "templates")), autoescape=True)
    html = env.get_template("downloads.html").render(
        request=Request(scope),
        downloads_set_up=body["downloads_set_up"],
        strip=body["strip"],
        can_abandon=True,
        **{key: body[key] for key in told},
    )
    names = re.findall(r'<button[^>]*data-abandon-trigger[^>]*aria-label="([^"]*)"', html)
    assert sorted(names) == [
        "Abandon search: Hero Film",
        "Abandon search: Queue Film",
        "Stop tracking: Later Film",
        "Stop tracking: Soon Film",
    ]
    # the tooltip keeps the plain label
    assert 'title="Stop tracking"' in html and 'title="Abandon search"' in html


def test_an_empty_title_leaves_no_trailing_colon_in_the_name() -> None:
    env = Environment(loader=FileSystemLoader(str(_WEB / "templates")))
    module = env.get_template("_components.html").make_module({"icon": lambda _n: ""})
    named = module.abandon_btn("radarr:1", "movie", "Hero Film")
    bare = module.abandon_btn("radarr:1", "movie", "")
    assert 'aria-label="Abandon search: Hero Film"' in named
    assert 'aria-label="Abandon search"' in bare
