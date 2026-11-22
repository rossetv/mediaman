"""Show keep log lines escape line breaks, and the unknown-show answer carries a message."""

from __future__ import annotations

import logging

from mediaman.web.routes import kept_show as ks
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


def _stored_season(conn, key):
    insert_media_item(
        conn,
        id="101",
        title="s1",
        media_type="tv_season",
        show_title="Show",
        season_number=1,
        show_rating_key=key,
        file_path="/media/tv/x",
    )


def test_unresolved_key_log_escapes_supplied_key(conn, caplog):
    with caplog.at_level(logging.WARNING):
        _, err = ks._validate_keep_show_input(conn, "9\r\nFORGED", "forever", ["1"], "admin")
    assert err is not None
    _forged(caplog)


def test_unowned_season_log_escapes_resolved_key(conn, caplog):
    _stored_season(conn, "100\r\nFORGED")
    insert_media_item(
        conn,
        id="102",
        title="s2",
        media_type="tv_season",
        show_title="Show",
        season_number=2,
        show_rating_key=None,
        file_path="/media/tv/y",
    )
    with caplog.at_level(logging.WARNING):
        err = ks._check_season_ownership(conn, ["101", "102"], "100\r\nFORGED", "admin")
    assert err is not None
    _forged(caplog)


def test_kept_show_log_escapes_resolved_key(conn, caplog, monkeypatch):
    _stored_season(conn, "100\r\nFORGED")
    monkeypatch.setattr(ks, "get_db", lambda: conn)
    body = ks._KeepShowBody(duration="forever", season_ids=["101"])
    with caplog.at_level(logging.INFO):
        resp = ks.api_keep_show("100\r\nFORGED", body, admin="admin")
    assert resp.status_code == 200
    _forged(caplog)
