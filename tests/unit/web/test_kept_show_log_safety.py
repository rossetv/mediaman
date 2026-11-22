"""Show keep log lines escape line breaks, and the unknown-show answer carries a message."""

from __future__ import annotations

import logging

from mediaman.web.routes.kept import router as kept_router
from tests.helpers.factories import insert_media_item


def _client(app_factory, authed_client, conn):
    return authed_client(app_factory(kept_router, conn=conn), conn)


def _forged(caplog):
    lines = [r.getMessage() for r in caplog.records if "FORGED" in r.getMessage()]
    assert lines
    assert not any("\r" in m or "\n" in m for m in lines)


def test_unknown_show_409_has_message(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)
    resp = client.post("/api/show/99/keep", json={"duration": "forever", "season_ids": ["1"]})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "Unknown show_rating_key"
    assert body["message"] == "Unknown show_rating_key"


def test_kept_and_removed_show_logs_escape_title_and_key(app_factory, authed_client, conn, caplog):
    insert_media_item(
        conn,
        id="101",
        title="s1",
        media_type="tv_season",
        show_title="Show\r\nFORGED",
        season_number=1,
        show_rating_key="100",
        file_path="/media/tv/x",
    )
    client = _client(app_factory, authed_client, conn)
    with caplog.at_level(logging.INFO):
        keep = client.post(
            "/api/show/100/keep", json={"duration": "forever", "season_ids": ["101"]}
        )
        assert keep.status_code == 200
    _forged(caplog)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        client.post("/api/show/100%0D%0AFORGED/remove")
        conn.execute("UPDATE kept_shows SET show_rating_key = ?", ("100\r\nFORGED",))
        conn.commit()
        client.post("/api/show/100%0D%0AFORGED/remove")
    _forged(caplog)
