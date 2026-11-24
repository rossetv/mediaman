"""Tests that tidying up after Plex's list never costs an item its keep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db, open_thread_connection
from mediaman.scanner import repository
from mediaman.scanner.engine import ScanEngine
from mediaman.web.repository.library_api import apply_keep_in_tx
from tests.helpers.factories import insert_media_item

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
                "season_number": None,
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


def _live_actions(conn):
    return {
        r["media_item_id"]: (r["action"], r["execute_at"])
        for r in conn.execute("SELECT media_item_id, action, execute_at FROM scheduled_actions")
    }


class TestListShorterThanTheLibrary:
    """Plex lists nothing while its media is unmounted, then everything again."""

    @pytest.fixture
    def kept(self, conn):
        _engine(conn, _Plex(_FILMS)).sync_library()
        until = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        for media_id in _FILMS[:-1]:
            _keep(conn, media_id)
        _keep(conn, _FILMS[-1], action="snoozed", execute_at=until)
        return until

    @pytest.mark.parametrize("key", ["{}", "new-{}"], ids=["same_key", "new_key"])
    @pytest.mark.parametrize("back", ["run_scan", "sync_library"])
    def test_kept_films_listed_again_are_still_kept(self, conn, kept, key, back):
        _engine(conn, _Plex([])).sync_library()
        _engine(conn, _Plex([])).sync_library()
        assert _live_actions(conn) == {}

        getattr(_engine(conn, _Plex([key.format(f) for f in _FILMS])), back)()
        _engine(conn, _Plex([key.format(f) for f in _FILMS])).run_scan()

        assert _live_actions(conn) == {
            **{key.format(f): ("protected_forever", None) for f in _FILMS[:-1]},
            key.format(_FILMS[-1]): ("snoozed", kept),
        }

    def test_a_kept_film_missing_from_one_list_is_still_kept(self, conn, kept):
        _engine(conn, _Plex(_FILMS[1:])).sync_library()

        _engine(conn, _Plex(_FILMS)).run_scan()

        assert _actions(conn, "o0") == ["protected_forever"]

    def test_a_kept_film_the_list_names_twice_is_still_kept(self, conn, kept):
        _engine(conn, _Plex(_FILMS[1:])).sync_library()

        _engine(conn, _Plex([*_FILMS, "o0"])).run_scan()

        assert _actions(conn, "o0") == ["protected_forever"]

    def test_a_film_whose_keep_ran_out_meanwhile_is_scheduled(self, conn, freezer, kept):
        freezer.tick(timedelta(hours=1))
        _engine(conn, _Plex(_FILMS[:-1])).sync_library()
        assert _actions(conn, _FILMS[-1]) == []
        freezer.tick(timedelta(days=31))

        _engine(conn, _Plex(_FILMS)).run_scan()

        assert _actions(conn, _FILMS[-1]) == ["scheduled_deletion"]

    def test_another_copy_still_listed_does_not_take_the_keep(self, conn, kept):
        _engine(conn, _Plex([*_FILMS, "new-o0"])).sync_library()
        _engine(conn, _Plex([*_FILMS[1:], "new-o0"])).sync_library()

        _engine(conn, _Plex([*_FILMS, "new-o0"])).sync_library()

        assert _actions(conn, "o0") == ["protected_forever"]
        assert _actions(conn, "new-o0") == []

    def test_a_season_takes_back_only_its_own_keep_when_seasons_share_a_folder(self, conn):
        for media_id, season in (("s1", 1), ("s2", 2)):
            insert_media_item(
                conn,
                id=media_id,
                plex_rating_key=media_id,
                media_type="tv_season",
                season_number=season,
                file_path="/tv/Show",
            )
        _keep(conn, "s2")
        with conn:
            repository.save_pruned_keeps(conn, ["s1", "s2"])
            repository.delete_actions_for_media_items(conn, ["s1", "s2"])
            repository.delete_media_items(conn, ["s1", "s2"])

        assert repository.take_pruned_keeps(conn, "n1", "/tv/Show", 1) == []
        assert len(repository.take_pruned_keeps(conn, "n2", "/tv/Show", 2)) == 1

    def test_looking_for_a_keep_to_give_back_does_not_read_every_keep_set_aside(self, conn):
        queries = []
        conn.set_trace_callback(queries.append)
        repository.take_pruned_keeps(conn, "o0", "/media/o0.mkv", None)
        conn.set_trace_callback(None)

        plan = [r["detail"] for q in queries if q.startswith("SELECT") for r in _plan(conn, q)]
        assert plan
        assert not [step for step in plan if step.startswith("SCAN")]


def _plan(conn, query):
    return conn.execute(f"EXPLAIN QUERY PLAN {query}").fetchall()
