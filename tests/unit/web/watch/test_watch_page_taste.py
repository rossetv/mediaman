"""The Watch page carries the signed-in person's own search taste, and none for someone who
switched off the use of their history."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.core.time import now_utc
from mediaman.services.watch_catalog import _title_meta
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings

_TEMPLATES = Path(mediaman.__file__).parent / "web" / "templates"
_ISLAND = re.compile(r'<script type="application/json" id="watch-page-data">(.*?)</script>', re.S)


@pytest.fixture
def client(app_factory, conn, monkeypatch):
    monkeypatch.setattr(_title_meta, "_library", {})
    insert_settings(conn, plex_libraries='["1"]')
    for tmdb_id, genre in ((1, "comedy"), (2, "comedy"), (3, "comedy"), (4, "romance")):
        insert_media_item(
            conn, id=str(tmdb_id), plex_rating_key=str(tmdb_id), title=f"Film {tmdb_id}",
            media_type="movie", plex_library_id=1,
        )  # fmt: skip
        conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = ?", (tmdb_id, str(tmdb_id)))
        details = {
            "title": f"Film {tmdb_id}", "released": "2020-01-01", "poster": None, "rating": 7.0,
            "votes": 100, "popularity": 1.0, "genres": [genre],
        }  # fmt: skip
        conn.execute(
            "INSERT INTO title_meta (id_type, id, tmdb_id, details, cast_json, fetched_at)"
            " VALUES ('movie', ?, ?, ?, '[]', '2026-01-01')",
            (tmdb_id, tmdb_id, json.dumps(details)),
        )
    conn.commit()
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATES))},
    )
    return TestClient(app)


def _played(conn, username, *tmdb_ids):
    at = (now_utc() - timedelta(days=1)).isoformat()
    for tmdb_id in tmdb_ids:
        conn.execute(
            "INSERT INTO played_titles (username, media_type, title, tmdb_id, played_at)"
            " VALUES (?, 'movie', ?, ?, ?)",
            (username, f"Film {tmdb_id}", tmdb_id, at),
        )
    conn.commit()


def _taste(client, conn, username):
    client.cookies.set("session_token", create_session(conn, username))
    resp = client.get("/watch")
    assert resp.status_code == 200
    (embedded,) = _ISLAND.findall(resp.text)
    return json.loads(embedded)["taste"]


def test_each_person_gets_their_own_order(client, conn):
    insert_admin_user(conn, username="ann", role="viewer")
    insert_admin_user(conn, username="bob", role="admin")
    _played(conn, "ann", 1, 2, 3)
    _played(conn, "bob", 1, 4)
    conn.execute(
        "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
        " SELECT id, 'movie', 4, 2, 'Film 4', '2026-01-01' FROM admin_users WHERE username = 'bob'"
    )
    _played(conn, "bob", 2)
    conn.commit()

    ann = _taste(client, conn, "ann")
    bob = _taste(client, conn, "bob")

    assert ann["personal"] is True and ann["order"][0] == "comedy"
    assert bob["personal"] is True and bob["order"][0] == "romance"


def test_none_is_sent_for_someone_who_switched_their_history_off(client, conn):
    insert_admin_user(conn, username="ann", role="viewer")
    _played(conn, "ann", 1, 2, 3)
    conn.execute("UPDATE admin_users SET ai_history = 0 WHERE username = 'ann'")
    conn.commit()

    assert _taste(client, conn, "ann") is None
