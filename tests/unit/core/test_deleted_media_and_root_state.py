"""deleted_media rows, audit ids and the delete-root state row."""

from __future__ import annotations

from mediaman.core.audit import log_audit
from mediaman.core.delete_root_state import (
    read_delete_root_state,
    read_delete_root_waiting,
    write_delete_root_state,
)
from mediaman.core.deleted_media import (
    fetch_deleted_media,
    fill_show_tvdb_id,
    record_deleted_media,
)
from mediaman.db import init_db
from tests.helpers.factories import insert_media_item, insert_scheduled_action


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


def test_a_learned_tvdb_id_goes_to_every_deletion_of_that_show_and_no_other(tmp_path):
    conn = init_db(str(tmp_path / "h.db"))
    deletions = [
        ("tv_season", 7, None, 1),
        ("anime_season", 7, None, 2),
        ("tv_season", 7, 4, 3),
        ("tv_season", 8, None, 1),
        ("movie", 7, None, None),
    ]
    ids = []
    for media_type, tmdb_id, tvdb_id, season in deletions:
        ids.append(log_audit(conn, "m", "deleted", "x"))
        record_deleted_media(
            conn,
            ids[-1],
            media_type=media_type,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            season_number=season,
            title="T",
        )

    fill_show_tvdb_id(conn, tmdb_id=7, tvdb_id=9)

    stored = fetch_deleted_media(conn, ids)
    assert [stored[i].tvdb_id for i in ids] == [9, 9, 4, None, None]
    conn.close()


def test_was_redownloaded_after_needs_a_newer_match():
    from mediaman.core.deleted_media import was_redownloaded_after

    times = {"tmdb:7": "2026-02-01", "other": "2026-03-01"}
    assert was_redownloaded_after("2026-01-01", ["tmdb:7"], times)
    assert not was_redownloaded_after("2026-02-01", ["tmdb:7"], times)
    assert not was_redownloaded_after("2026-01-01", ["tmdb:8"], times)


def _waiting_action(conn):
    insert_media_item(conn, id="m1", title="F", plex_rating_key="rk1")
    return insert_scheduled_action(conn, media_item_id="m1", token="t1")


def test_waiting_ids_out_of_int64_range_fall_back_to_stored_count(tmp_path):
    conn = init_db(str(tmp_path / "e.db"))
    write_delete_root_state(conn, {"/a": 4}, {"/a": [2**70]})
    assert read_delete_root_waiting(conn) == {"/a": 4}
    conn.close()


def test_waiting_ids_that_are_not_a_list_of_ints_fall_back_to_stored_count(tmp_path):
    conn = init_db(str(tmp_path / "f.db"))
    real = _waiting_action(conn)
    for bad in ("123", [real, "1"], [real, True], [real, 1.5], {"1": 1}):
        write_delete_root_state(conn, {"/a": 4}, {"/a": bad})
        assert read_delete_root_waiting(conn) == {"/a": 4}, bad
    conn.close()


def test_waiting_counts_with_more_ids_than_sqlite_binds(tmp_path):
    conn = init_db(str(tmp_path / "g.db"))
    real = _waiting_action(conn)
    ids = [real, *range(real + 1, real + 40_001)]
    write_delete_root_state(conn, {"/a": len(ids)}, {"/a": ids})
    assert read_delete_root_waiting(conn) == {"/a": 1}
    conn.close()


def test_waiting_falls_back_to_stored_counts_when_the_count_query_fails(tmp_path):
    conn = init_db(str(tmp_path / "h.db"))
    write_delete_root_state(conn, {"/a": 2}, {"/a": [1, 2]})
    conn.execute("DROP TABLE scheduled_actions")
    assert read_delete_root_waiting(conn) == {"/a": 2}
    conn.close()
