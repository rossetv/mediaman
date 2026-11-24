"""Tests that tidying up after Plex's list never costs an item its keep."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediaman.db import init_db, open_thread_connection
from mediaman.scanner.engine import ScanEngine
from mediaman.web.repository.library_api import apply_keep_in_tx

_OLD = datetime(2020, 1, 1, tzinfo=UTC)
_FILMS = [f"o{i}" for i in range(6)]


class _Plex:
    """A movie library listing *films*; *on_history* runs once, after the list was read."""

    def __init__(self, films, on_history=None):
        self._films = films
        self._on_history = on_history

    def get_movie_items(self, _library_id):
        return [
            {
                "plex_rating_key": key,
                "title": key,
                "added_at": _OLD,
                "file_path": f"/media/{key.removeprefix('new-')}.mkv",
                "file_size_bytes": 1,
            }
            for key in self._films
        ]

    def get_watch_history(self, _key):
        hook, self._on_history = self._on_history, None
        if hook:
            hook()
        return []


@pytest.fixture
def conn(db_path, monkeypatch):
    monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
    monkeypatch.setattr("mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None)
    return init_db(str(db_path))


def _engine(conn, plex):
    return ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=["1"],
        library_types={"1": "movie"},
        secret_key="0123456789abcdef" * 4,
    )


def _keep(conn, media_id, *, action="protected_forever", execute_at=None):
    apply_keep_in_tx(
        conn,
        media_id=media_id,
        action=action,
        execute_at=execute_at,
        now_iso=datetime.now(UTC).isoformat(),
        snooze_label="forever",
        new_token=f"tok-{media_id}",
        audit_detail="Kept",
        actor="admin",
    )


def _actions(conn, media_id):
    return [
        r[0]
        for r in conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
        )
    ]


class TestListOlderThanAKeep:
    @pytest.mark.parametrize("run", ["sync_library", "run_scan"])
    def test_a_film_that_came_and_was_kept_during_the_run_stays_kept(self, conn, db_path, run):
        def film_arrives_and_is_kept():
            other = open_thread_connection(str(db_path))
            _engine(other, _Plex([*_FILMS, "new"])).sync_library()
            _keep(other, "new")
            other.close()

        getattr(_engine(conn, _Plex(_FILMS, film_arrives_and_is_kept)), run)()

        assert _actions(conn, "new") == ["protected_forever"]

    def test_a_film_kept_during_the_run_stays_though_only_the_keep_is_new(self, conn, db_path):
        _engine(conn, _Plex([*_FILMS, "new"])).sync_library()

        def kept():
            other = open_thread_connection(str(db_path))
            _keep(other, "new")
            other.close()

        _engine(conn, _Plex(_FILMS, kept)).sync_library()

        assert _actions(conn, "new") == ["protected_forever"]

    def test_a_film_plex_dropped_goes_with_the_next_run(self, conn):
        _engine(conn, _Plex([*_FILMS, "gone"])).sync_library()

        _engine(conn, _Plex(_FILMS)).sync_library()

        assert conn.execute("SELECT 1 FROM media_items WHERE id = 'gone'").fetchone() is None
