"""deleted_media rows, audit ids and the delete-root state row."""

from __future__ import annotations

from mediaman.core.audit import log_audit
from mediaman.core.delete_root_state import read_delete_root_state, write_delete_root_state
from mediaman.core.deleted_media import fetch_deleted_media, record_deleted_media
from mediaman.db import init_db


def test_log_audit_returns_distinct_ids(tmp_path):
    conn = init_db(str(tmp_path / "a.db"))
    a = log_audit(conn, "m1", "deleted", "x")
    b = log_audit(conn, "m2", "deleted", "y")
    assert isinstance(a, int) and b == a + 1
    conn.close()


def test_record_and_fetch_deleted_media(tmp_path):
    conn = init_db(str(tmp_path / "b.db"))
    audit_id = log_audit(conn, "m1", "deleted", "x")
    record_deleted_media(
        conn, audit_id, media_type="season", tmdb_id=None, tvdb_id=7, season_number=2, title="Show"
    )
    got = fetch_deleted_media(conn, [audit_id, audit_id + 99])
    assert list(got) == [audit_id]
    assert (got[audit_id].tvdb_id, got[audit_id].season_number) == (7, 2)
    assert fetch_deleted_media(conn, []) == {}
    conn.close()


def test_root_state_round_trip_and_clear(tmp_path):
    conn = init_db(str(tmp_path / "c.db"))
    assert read_delete_root_state(conn) == {}
    write_delete_root_state(conn, {"/media": 3})
    write_delete_root_state(conn, {"/media": 4, "/tv": 1})
    assert read_delete_root_state(conn) == {"/media": 4, "/tv": 1}
    write_delete_root_state(conn, {})
    assert read_delete_root_state(conn) == {}
    conn.close()


def test_root_state_malformed_is_empty(tmp_path):
    conn = init_db(str(tmp_path / "d.db"))
    for bad in ("not json", "[]", '{"roots": 5}', '{"roots": {"/a": "x"}}'):
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            "VALUES ('delete_root_unavailable', ?, 'now')",
            (bad,),
        )
        assert read_delete_root_state(conn) == {}
    conn.close()


def test_redownload_tokens_by_media_type():
    from mediaman.core.deleted_media import DeletedMedia, redownload_tokens

    movie = DeletedMedia(1, "movie", 7, None, None, "M")
    season = DeletedMedia(2, "season", None, 9, 3, "S")
    assert redownload_tokens(movie) == ["tmdb:7"]
    assert redownload_tokens(season) == ["tvdb:9", "tvdb:9:s3"]
    assert redownload_tokens(DeletedMedia(3, "movie", None, None, None, "M")) == []
    assert redownload_tokens(None) == []


def test_was_redownloaded_after_needs_a_newer_match():
    from mediaman.core.deleted_media import was_redownloaded_after

    times = {"tmdb:7": "2026-02-01", "other": "2026-03-01"}
    assert was_redownloaded_after("2026-01-01", ["tmdb:7"], times)
    assert not was_redownloaded_after("2026-02-01", ["tmdb:7"], times)
    assert not was_redownloaded_after("2026-01-01", ["tmdb:8"], times)
