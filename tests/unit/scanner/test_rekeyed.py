"""Tests that a title Plex gives a new key keeps its show's keep and everyone's progress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import requests

from mediaman.core.audit import log_audit
from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.scanner.repository.watch_credit import carry_last_watched
from mediaman.web.auth.password_hash import delete_user
from mediaman.web.repository.kept import upsert_kept_show
from mediaman.web.repository.watch_progress import save_progress
from tests.helpers.factories import insert_kept_show

_OLD = datetime(2020, 1, 1, tzinfo=UTC)
_NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def _film(key, path=None):
    return {
        "plex_rating_key": key,
        "title": key,
        "added_at": _OLD,
        "file_path": f"/m/{path or key.removeprefix('new-')}.mkv",
        "file_size_bytes": 1,
    }


def _season(show, number, *, prefix="", folder=None, files="", episodes=(1, 2)):
    """Season *number* of *show*, with keys starting *prefix* and its files where *show* has them.

    *files* starts the name of each.
    """
    folder = f"/tv/{folder or show}/Season {number}"
    return {
        "plex_rating_key": f"{prefix}{show}-s{number}",
        "title": show,
        "show_title": show,
        "season_number": number,
        "added_at": _OLD,
        "file_path": folder,
        "file_size_bytes": 1,
        "show_rating_key": f"{prefix}{show}",
        "episode_count": len(episodes),
        "episodes": [
            {
                "key": f"{prefix}{show}-s{number}e{e}",
                "number": e,
                "path": f"{folder}/{files}E{e}.mkv",
            }
            for e in episodes
        ],
    }


class _Plex:
    """Lists *films* and *seasons*; *watched* says when the plays of each key were."""

    def __init__(self, films=(), seasons=(), no_history=(), watched=()):
        self._films, self._seasons, self._no_history = films, seasons, no_history
        self._watched = dict(watched)

    def get_movie_items(self, _library_id):
        return list(self._films)

    def get_show_seasons(self, _library_id):
        return list(self._seasons)

    def get_watch_history(self, key):
        return [{"viewed_at": self._watched[key]}] if key in self._watched else []

    def get_season_watch_history(self, key):
        if key in self._no_history:
            raise requests.ConnectionError
        return self.get_watch_history(key)


@pytest.fixture
def conn(db_path, monkeypatch, freezer):
    freezer.move_to(_NOW)
    monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
    monkeypatch.setattr("mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None)
    conn = init_db(str(db_path))
    conn.executemany(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'h', '2020')",
        [("amy",), ("bo",)],
    )
    conn.commit()
    return conn


def _engine(conn, plex, library_types=None):
    library_types = library_types or {"1": "movie", "2": "show"}
    return ScanEngine(
        conn=conn,
        plex_client=plex,
        library_ids=list(library_types),
        library_types=library_types,
        secret_key="0123456789abcdef" * 4,
    )


def _sync(conn, **plex):
    _engine(conn, _Plex(**plex)).sync_library()


def _scan(conn, **plex):
    _engine(conn, _Plex(**plex)).run_scan()


def _kept(conn):
    return {
        r["show_rating_key"]: (r["action"], r["execute_at"])
        for r in conn.execute("SELECT * FROM kept_shows")
    }


def _scheduled(conn):
    return {
        r["media_item_id"]
        for r in conn.execute(
            "SELECT media_item_id FROM scheduled_actions WHERE action = 'scheduled_deletion'"
        )
    }


def _keep(conn, show, *, action="protected_forever", execute_at=None):
    insert_kept_show(
        conn,
        show_rating_key=show,
        show_title=show,
        action=action,
        execute_at=execute_at,
        created_at=_NOW.isoformat(),
    )


def _remove_keep(conn, show):
    with conn:
        conn.execute("DELETE FROM kept_shows WHERE show_rating_key = ?", (show,))
        log_audit(conn, show, "removed_show_keep", "removed", actor="admin")


FOREVER = ("protected_forever", None)
_SHOW = [_season("show", 1)]
# A season nobody had before comes first, so it is evaluated before the one that is known.
_REKEYED = [_season("show", 2, prefix="new-"), _season("show", 1, prefix="new-")]

# What a season of a kept show and one of another show in its folder say they are.
_SHARED_FOLDER = pytest.mark.parametrize(
    ("kept", "other"),
    [
        ({}, {}),
        ({"tmdb_id": 1}, {"tmdb_id": 2, "show_title": "show"}),
        ({"tvdb_id": 1}, {"tvdb_id": 2, "show_title": "show"}),
    ],
)


def _keys_of(season):
    return {key: season[key] for key in ("plex_rating_key", "show_rating_key", "episodes")}


_RUNS = pytest.mark.parametrize("run", [_sync, _scan], ids=["sync", "scan"])


class TestWholeShowKeep:
    @_RUNS
    @pytest.mark.parametrize("empty_lists", [0, 1, 2])
    def test_no_season_of_a_kept_show_is_scheduled_under_its_new_key(
        self, conn, freezer, run, empty_lists
    ):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        run(conn, seasons=_REKEYED)
        _scan(conn, seasons=_REKEYED)

        assert _scheduled(conn) == set()
        assert _kept(conn)["new-show"] == FOREVER

    @_RUNS
    def test_the_keep_reaches_a_new_key_listed_beside_the_old_one_at_first(
        self, conn, freezer, run
    ):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        _sync(conn, seasons=[*_SHOW, _season("show", 1, prefix="new-")])
        assert "new-show" not in _kept(conn)

        freezer.tick(60)
        run(conn, seasons=[_season("show", 1, prefix="new-")])

        assert _kept(conn)["new-show"] == FOREVER

    @_RUNS
    def test_seasons_scheduled_beside_the_old_key_are_not_once_it_goes(self, conn, freezer, run):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        _scan(conn, seasons=[*_SHOW, *_REKEYED])
        assert _scheduled(conn) == {"new-show-s1", "new-show-s2"}

        freezer.tick(60)
        run(conn, seasons=_REKEYED)

        assert _scheduled(conn) == set()

    @_RUNS
    def test_a_season_scheduled_before_the_known_one_is_listed_is_not_after(
        self, conn, freezer, run
    ):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        _scan(conn, seasons=[_season("show", 2, prefix="new-")])
        assert _scheduled(conn) == {"new-show-s2"}

        freezer.tick(60)
        run(conn, seasons=_REKEYED)

        assert _scheduled(conn) == set()

    @_RUNS
    def test_a_keep_the_new_key_already_has_does_not_leave_its_seasons_scheduled(
        self, conn, freezer, run
    ):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show", action="snoozed", execute_at=(_NOW + timedelta(days=9)).isoformat())
        _scan(conn, seasons=[*_SHOW, *_REKEYED])
        _keep(conn, "new-show")
        assert _scheduled(conn) == {"new-show-s1", "new-show-s2"}

        freezer.tick(60)
        run(conn, seasons=_REKEYED)

        assert _scheduled(conn) == set()

    def test_a_season_already_being_deleted_keeps_its_row_for_the_pass(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        _scan(conn, seasons=[*_SHOW, *_REKEYED])
        with conn:
            conn.execute(
                "UPDATE scheduled_actions SET delete_status = 'deleting' "
                "WHERE media_item_id = 'new-show-s2'"
            )

        freezer.tick(60)
        _sync(conn, seasons=_REKEYED)

        assert _scheduled(conn) == {"new-show-s2"}

    def test_a_season_scheduled_after_the_keep_was_removed_from_the_new_key_stays_so(
        self, conn, freezer
    ):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        freezer.tick(60)
        _sync(conn, seasons=_REKEYED)
        freezer.tick(60)
        _remove_keep(conn, "new-show")
        freezer.tick(60)
        _scan(conn, seasons=[*_SHOW, *_REKEYED])
        assert _scheduled(conn) == {"new-show-s1", "new-show-s2"}

        freezer.tick(60)
        _scan(conn, seasons=_REKEYED)

        assert _scheduled(conn) == {"new-show-s1", "new-show-s2"}

    def test_the_other_seasons_stay_when_the_known_one_has_no_history(self, conn):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")

        _scan(conn, seasons=_REKEYED, no_history=["new-show-s1"])

        assert _scheduled(conn) == set()

    def test_the_keep_follows_a_season_plex_moves_to_another_show_key(self, conn):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")

        _scan(conn, seasons=[{**_SHOW[0], "show_rating_key": "merged"}])

        assert _scheduled(conn) == set()
        assert _kept(conn)["merged"] == FOREVER

    def test_the_same_season_of_another_show_is_not_kept(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        _sync(conn)
        freezer.tick(60)
        _sync(conn)

        _scan(conn, seasons=[_season("other", 1), _season("show", 3, prefix="x-")])

        assert _scheduled(conn) == {"other-s1", "x-show-s3"}

    @_RUNS
    @_SHARED_FOLDER
    def test_another_show_in_its_folder_is_not_kept_as_it_goes(
        self, conn, freezer, run, kept, other
    ):
        kept, other = (
            _season("show", 1) | kept,
            _season("other", 1, folder="show", files="o") | other,
        )
        _sync(conn, seasons=[kept])
        _keep(conn, "show")
        _scan(conn, seasons=[kept, other])
        assert _scheduled(conn) == {"other-s1"}

        freezer.tick(60)
        run(conn, seasons=[other])

        assert "other" not in _kept(conn)
        assert _scheduled(conn) == {"other-s1"}

    @_SHARED_FOLDER
    def test_another_show_new_in_its_folder_is_not_kept(self, conn, kept, other):
        _sync(conn, seasons=[_season("show", 1) | kept])
        _keep(conn, "show")

        _scan(conn, seasons=[_season("other", 1, folder="show", files="o") | other])

        assert "other" not in _kept(conn)
        assert _scheduled(conn) == {"other-s1"}

    def test_a_show_where_it_was_is_kept_once_nothing_tells_them_apart(self, conn, freezer):
        _sync(conn, seasons=[_season("show", 1) | {"tmdb_id": 1}])
        _set_watched(conn, "show-s1", _NOW)
        _keep(conn, "show")
        for _ in range(2):
            freezer.tick(60)
            _sync(conn)

        _scan(conn, seasons=[_season("other", 1, folder="show") | {"tmdb_id": 2}])

        assert _kept(conn)["other"] == FOREVER

    @_SHARED_FOLDER
    def test_a_show_in_its_folder_does_not_lose_it_the_keep_under_its_new_key(
        self, conn, freezer, kept, other
    ):
        kept, other = (
            _season("show", 1) | kept,
            _season("other", 1, folder="show", files="o") | other,
        )
        _sync(conn, seasons=[kept, other])
        _keep(conn, "show")
        for _ in range(2):
            freezer.tick(60)
            _sync(conn, seasons=[other])

        freezer.tick(60)
        _scan(conn, seasons=[other, kept | _keys_of(_season("show", 1, prefix="new-"))])

        assert _kept(conn)["new-show"] == FOREVER
        assert _scheduled(conn) == {"other-s1"}

    @_RUNS
    @pytest.mark.parametrize("beside", [True, False])
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param({}, {}, id="no_ids"),
            pytest.param({"tmdb_id": 1}, {"tmdb_id": 2}, id="match_corrected"),
            pytest.param({"episodes": []}, {}, id="old_files_unknown"),
            pytest.param({}, {"episodes": []}, id="new_files_unknown"),
            pytest.param(
                {"tmdb_id": 1},
                {"tmdb_id": 1, "episodes": _season("show", 1, files="new")["episodes"]},
                id="files_replaced",
            ),
            pytest.param(
                {"episodes": [], "tracked_files": [{"path": "/tv/E1.mkv", "size": 1}]},
                {"episodes": [], "tracked_files": [{"path": "/tv/E1.mkv", "size": 1}]},
                id="tracked_files_only",
            ),
        ],
    )
    def test_a_renamed_show_is_kept_under_its_new_key(self, conn, freezer, run, beside, old, new):
        old = _season("show", 1) | old
        new = _season("show", 1, prefix="new-") | {"show_title": "Show (2020)"} | new
        _sync(conn, seasons=[old])
        _keep(conn, "show")
        if beside:
            _sync(conn, seasons=[old, new])

        freezer.tick(60)
        run(conn, seasons=[new])

        assert _kept(conn)["new-show"] == FOREVER
        assert _scheduled(conn) == set()

    def test_a_copy_listed_beside_the_kept_show_is_not_kept(self, conn):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")

        _scan(conn, seasons=[_season("show", 1, prefix="copy-"), *_SHOW])

        assert _scheduled(conn) == {"copy-show-s1"}

    def test_a_keep_for_a_while_ends_when_it_did(self, conn, freezer):
        until = (_NOW + timedelta(days=30)).isoformat()
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show", action="snoozed", execute_at=until)

        _sync(conn, seasons=_REKEYED)
        assert _kept(conn)["new-show"] == ("snoozed", until)

        freezer.tick(timedelta(days=31))
        _scan(conn, seasons=_REKEYED)
        assert _scheduled(conn) == {"new-show-s1", "new-show-s2"}

    def test_a_longer_keep_the_new_key_already_has_is_not_cut_short(self, conn):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show", action="snoozed", execute_at=(_NOW + timedelta(days=9)).isoformat())
        _keep(conn, "new-show")

        _sync(conn, seasons=_REKEYED)

        assert _kept(conn)["new-show"] == FOREVER

    def test_the_old_key_is_still_kept_if_it_comes_back(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        freezer.tick(60)
        _sync(conn, seasons=_REKEYED)

        freezer.tick(60)
        _scan(conn, seasons=[*_SHOW, *_REKEYED])

        assert _scheduled(conn) == set()

    def test_a_keep_removed_from_the_new_key_does_not_return_to_it(self, conn, freezer):
        others = [_season("other", n) for n in range(1, 6)]
        _sync(conn, seasons=[*_SHOW, _season("show", 2), *others])
        _keep(conn, "show")
        freezer.tick(60)
        _sync(conn, seasons=[_season("show", 1, prefix="new-"), *others])
        freezer.tick(60)
        _remove_keep(conn, "new-show")

        freezer.tick(60)
        _sync(conn, seasons=[*_REKEYED, *others])

        assert "new-show" not in _kept(conn)

    def test_a_keep_removed_from_the_new_key_is_not_given_to_the_next_one(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        for seasons in ([], [], [_season("show", 1, prefix="new-")]):
            freezer.tick(60)
            _sync(conn, seasons=seasons)
        freezer.tick(60)
        _remove_keep(conn, "new-show")

        _sync(conn, seasons=[_season("show", 1, prefix="newer-")])

        assert "newer-show" not in _kept(conn)

    def test_a_keep_a_returning_key_lost_before_is_not_given_to_the_next_one(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _keep(conn, "show")
        freezer.tick(60)
        _remove_keep(conn, "new-show")
        for seasons in ([], [], [_season("show", 1, prefix="new-")]):
            freezer.tick(60)
            _sync(conn, seasons=seasons)
        assert "new-show" not in _kept(conn)

        _sync(conn, seasons=[_season("show", 1, prefix="newer-")])

        assert "newer-show" not in _kept(conn)

    def test_a_show_kept_again_after_that_is_kept_under_the_new_key(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _remove_keep(conn, "new-show")
        freezer.tick(60)
        with conn:
            upsert_kept_show(
                conn,
                show_rating_key="show",
                show_title="show",
                action="protected_forever",
                execute_at=None,
                snooze_duration="forever",
                created_at=_OLD.isoformat(),
            )
            log_audit(conn, "show", "kept_show", "kept", actor="admin")

        _scan(conn, seasons=_REKEYED)

        assert _scheduled(conn) == set()


def _play(conn, rating_key, *, user="amy", media=None, show=None, at=_NOW, position=600.0):
    save_progress(
        conn,
        username=user,
        rating_key=rating_key,
        media_item_id=media or rating_key,
        show_rating_key=show,
        position_s=position,
        duration_s=6000.0,
        played_s=600.0,
        now=at,
    )


def _set_watched(conn, media_id, at):
    with conn:
        conn.execute(
            "UPDATE media_items SET last_watched_at = ? WHERE id = ?", (at.isoformat(), media_id)
        )


def _watched(conn, media_id):
    row = conn.execute(
        "SELECT last_watched_at FROM media_items WHERE id = ?", (media_id,)
    ).fetchone()
    return row["last_watched_at"]


def _progress(conn):
    return {
        (r["username"], r["rating_key"]): (
            r["media_item_id"],
            r["show_rating_key"],
            r["position_s"],
        )
        for r in conn.execute("SELECT * FROM watch_progress")
    }


_FILM = [_film("f1")]
_NEW_FILM = [_film("new-f1")]


class TestProgress:
    @_RUNS
    @pytest.mark.parametrize("empty_lists", [0, 1, 2])
    def test_everyone_keeps_their_place_in_a_film_under_its_new_key(
        self, conn, freezer, run, empty_lists
    ):
        _sync(conn, films=[*_FILM, _film("f2")])
        _play(conn, "f1", position=100.0)
        _play(conn, "f1", user="bo", position=200.0)
        _play(conn, "f2", position=300.0)
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        run(conn, films=[*_NEW_FILM, _film("f2")])

        assert _progress(conn) == {
            ("amy", "new-f1"): ("new-f1", None, 100.0),
            ("bo", "new-f1"): ("new-f1", None, 200.0),
            ("amy", "f2"): ("f2", None, 300.0),
        }

    @_RUNS
    @pytest.mark.parametrize("empty_lists", [0, 1, 2])
    def test_everyone_keeps_their_place_in_each_episode_under_its_new_key(
        self, conn, freezer, run, empty_lists
    ):
        _sync(conn, seasons=_SHOW)
        _play(conn, "show-s1e1", media="show-s1", show="show", position=100.0)
        _play(conn, "show-s1e2", media="show-s1", show="show", position=200.0)
        _play(conn, "show-s1e2", user="bo", media="show-s1", show="show", position=300.0)
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        run(conn, seasons=_REKEYED)

        home = ("new-show-s1", "new-show")
        assert _progress(conn) == {
            ("amy", "new-show-s1e1"): (*home, 100.0),
            ("amy", "new-show-s1e2"): (*home, 200.0),
            ("bo", "new-show-s1e2"): (*home, 300.0),
        }

    def test_the_old_key_listed_again_gets_its_progress_back(self, conn, freezer):
        _sync(conn, films=_FILM, seasons=[{**_SHOW[0], "episodes": []}])
        _play(conn, "f1")
        _play(conn, "show-s1e1", media="show-s1", show="show")
        before = _progress(conn)
        freezer.tick(60)
        _sync(conn)
        _sync(conn)
        assert _progress(conn) == {}

        _sync(conn, films=_FILM, seasons=_SHOW)

        assert _progress(conn) == before

    @pytest.mark.parametrize(
        ("old_at", "new_at", "position"),
        [(_NOW, _OLD, 100.0), (_OLD, _NOW, 200.0)],
        ids=["old_later", "new_later"],
    )
    def test_of_two_places_the_later_stays_with_the_mark_and_play_of_either(
        self, conn, freezer, old_at, new_at, position
    ):
        _sync(conn, films=[*_FILM, *_NEW_FILM])
        _play(conn, "f1", at=old_at, position=100.0)
        _play(conn, "new-f1", at=new_at, position=200.0)
        with conn:
            conn.execute(
                "UPDATE watch_progress SET seen_at = '2025-01-01', credited_at = NULL "
                "WHERE updated_at = ?",
                (min(old_at, new_at).isoformat(),),
            )
        freezer.tick(60)
        _sync(conn, films=_NEW_FILM)

        [row] = conn.execute("SELECT * FROM watch_progress").fetchall()
        assert (row["rating_key"], row["position_s"]) == ("new-f1", position)
        assert row["updated_at"] == _NOW.isoformat()
        assert (row["seen_at"], row["credited_at"]) == ("2025-01-01", _NOW.isoformat())

    def test_the_mark_and_play_of_the_old_key_are_not_lost_to_a_later_place(self, conn, freezer):
        _sync(conn, films=[*_FILM, *_NEW_FILM])
        _play(conn, "f1", at=_OLD)
        _play(conn, "new-f1")
        with conn:
            conn.execute("UPDATE watch_progress SET credited_at = NULL WHERE rating_key = 'new-f1'")
        freezer.tick(60)
        _sync(conn, films=_NEW_FILM)

        [row] = conn.execute("SELECT credited_at FROM watch_progress").fetchall()
        assert row["credited_at"] == _OLD.isoformat()

    @pytest.mark.parametrize("empty_lists", [0, 2])
    def test_a_title_hidden_from_continue_watching_stays_hidden(self, conn, freezer, empty_lists):
        _sync(conn, films=_FILM, seasons=_SHOW)
        _play(conn, "f1")
        _play(conn, "show-s1e1", media="show-s1", show="show")
        with conn:
            conn.executemany(
                "INSERT INTO watch_dismissed VALUES (?, ?, ?)",
                [
                    ("amy", "f1", "2026-11-25"),
                    ("amy", "show", "2026-11-26"),
                    ("bo", "show", "2026-11-27"),
                    ("amy", "new-show", "2026-11-01"),
                ],
            )
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        _sync(conn, films=_NEW_FILM, seasons=_REKEYED)

        hidden = conn.execute("SELECT * FROM watch_dismissed WHERE username = 'amy'").fetchall()
        assert sorted((r["title_key"], r["dismissed_at"]) for r in hidden) == [
            ("new-f1", "2026-11-25"),
            ("new-show", "2026-11-26"),
        ]

    @pytest.mark.parametrize("episode", [{"number": 7}, {"path": "/tv/show/Season 1/x.mkv"}])
    def test_an_episode_of_another_number_or_file_does_not_take_the_progress(
        self, conn, freezer, episode
    ):
        _sync(conn, seasons=_SHOW)
        _play(conn, "show-s1e1", media="show-s1", show="show")
        new = _season("show", 1, prefix="new-")
        new["episodes"][0].update(episode)

        freezer.tick(60)
        _sync(conn, seasons=[new])

        assert _progress(conn) == {}

    @pytest.mark.parametrize("empty_lists", [0, 2])
    def test_progress_two_titles_could_take_waits_for_one(self, conn, freezer, empty_lists):
        _sync(conn, films=_FILM, seasons=_SHOW)
        _play(conn, "f1")
        _play(conn, "show-s1e1", media="show-s1", show="show")
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        copies = [_season("show", 1, prefix="a-"), _season("show", 1, prefix="b-")]
        films = [_film("a", "f1"), _film("b", "f1")]
        freezer.tick(60)
        _sync(conn, films=films, seasons=copies)
        assert _progress(conn) == {}
        freezer.tick(60)
        _sync(conn, films=films[:1], seasons=copies[:1])

        assert set(_progress(conn)) == {("amy", "a"), ("amy", "a-show-s1e1")}

    def test_an_episode_without_a_number_takes_nothing(self, conn, freezer):
        old, new = _season("show", 1), _season("show", 1, prefix="new-")
        old["episodes"][0]["number"] = new["episodes"][0]["number"] = None
        _sync(conn, seasons=[old])
        _play(conn, "show-s1e1", media="show-s1", show="show")

        freezer.tick(60)
        _sync(conn, seasons=[new])

        assert _progress(conn) == {}

    def test_two_episodes_of_one_number_and_file_take_nothing(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _play(conn, "show-s1e1", media="show-s1", show="show")
        new = _season("show", 1, prefix="new-")
        new["episodes"].append({**new["episodes"][0], "key": "twin"})

        freezer.tick(60)
        _sync(conn, seasons=[new])

        assert _progress(conn) == {}

    def test_an_episode_under_a_new_key_in_the_same_season_keeps_its_place(self, conn, freezer):
        _sync(conn, seasons=_SHOW)
        _play(conn, "show-s1e1", media="show-s1", show="show", position=100.0)
        _play(conn, "e9", media="show-s1", show="show", at=_OLD, position=200.0)
        new = _season("show", 1)
        new["episodes"][0]["key"] = "e9"
        new["show_rating_key"] = "new-show"

        _sync(conn, seasons=[new])

        assert _progress(conn) == {("amy", "e9"): ("show-s1", "new-show", 100.0)}

    @_RUNS
    def test_a_film_that_is_gone_leaves_continue_watching(self, conn, freezer, run):
        _sync(conn, films=[*_FILM, _film("f2")])
        _play(conn, "f1")
        freezer.tick(60)

        run(conn, films=[_film("f2")])

        assert _progress(conn) == {}

    def test_a_film_mediaman_deleted_comes_back_unwatched(self, conn, freezer):
        _sync(conn, films=[*_FILM, _film("f2")])
        _play(conn, "f1")
        freezer.tick(60)
        with conn:
            log_audit(conn, "f1", "deleted", "Deleted: f1")
        _sync(conn, films=[_film("f2")])

        _sync(conn, films=[_film("f2"), *_NEW_FILM])

        assert _progress(conn) == {}

    def test_a_film_whose_path_lists_another_key_after_a_removal_keeps_its_place(
        self, conn, freezer
    ):
        _sync(conn, films=_FILM)
        _play(conn, "f1")
        freezer.tick(60)
        with conn:
            log_audit(conn, "f1", "deleted", "Deleted: f1")

        _sync(conn, films=_NEW_FILM)

        assert set(_progress(conn)) == {("amy", "new-f1")}

    def test_a_film_gone_for_months_comes_back_unwatched(self, conn, freezer):
        _sync(conn, films=[*_FILM, _film("f2")])
        _play(conn, "f1")
        freezer.tick(60)
        _sync(conn, films=[_film("f2")])
        freezer.tick(timedelta(days=91))
        _sync(conn, films=[_film("f3")])

        _sync(conn, films=[_film("f3"), *_NEW_FILM])

        assert _progress(conn) == {}

    def test_what_is_set_aside_for_a_person_goes_with_them(self, conn, freezer):
        _sync(conn, films=[*_FILM, _film("f2")])
        _play(conn, "f1", user="bo")
        freezer.tick(60)
        _sync(conn, films=[_film("f2")])
        assert delete_user(conn, 2, "amy")

        assert conn.execute("SELECT count(*) FROM pruned_progress").fetchone()[0] == 0


_YESTERDAY = _NOW - timedelta(days=1)
_LONG_AGO = _NOW - timedelta(days=200)


def _watched_in_plex(conn):
    _set_watched(conn, "f1", _YESTERDAY)
    _set_watched(conn, "show-s1", _YESTERDAY)


def _played_in_mediaman(conn):
    _play(conn, "f1", at=_YESTERDAY)
    _play(conn, "show-s1e2", media="show-s1", show="show", at=_YESTERDAY)


class TestLastWatched:
    @_RUNS
    @pytest.mark.parametrize("watch", [_watched_in_plex, _played_in_mediaman])
    @pytest.mark.parametrize("empty_lists", [0, 1, 2])
    def test_a_title_watched_lately_is_not_scheduled_under_its_new_key(
        self, conn, freezer, run, watch, empty_lists
    ):
        _sync(conn, films=_FILM, seasons=_SHOW)
        watch(conn)
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        new = {"films": _NEW_FILM, "seasons": [_season("show", 1, prefix="new-")]}
        run(conn, **new)

        assert _watched(conn, "new-f1") == _YESTERDAY.isoformat()
        assert _watched(conn, "new-show-s1") == _YESTERDAY.isoformat()
        _scan(conn, **new)
        assert _scheduled(conn) == set()

    def test_the_same_key_listed_again_is_still_watched(self, conn, freezer):
        _sync(conn, films=_FILM)
        _set_watched(conn, "f1", _YESTERDAY)
        for films in ([], []):
            freezer.tick(60)
            _sync(conn, films=films)

        _scan(conn, films=_FILM)

        assert _scheduled(conn) == set()

    def test_the_same_key_listed_again_at_another_path_is_still_watched(self, conn, freezer):
        _sync(conn, films=_FILM)
        _set_watched(conn, "f1", _YESTERDAY)
        for films in ([], []):
            freezer.tick(60)
            _sync(conn, films=films)

        _scan(conn, films=[_film("f1", "moved")])

        assert _scheduled(conn) == set()

    def test_a_new_key_first_listed_beside_the_old_one_is_not_scheduled(self, conn):
        _sync(conn, films=_FILM)
        _set_watched(conn, "f1", _YESTERDAY)

        _scan(conn, films=[*_NEW_FILM, *_FILM])

        assert "new-f1" not in _scheduled(conn)

    @pytest.mark.parametrize("known", [True, False])
    @pytest.mark.parametrize("new_first", [True, False])
    def test_a_watch_of_the_old_key_listed_beside_it_counts_whichever_is_first(
        self, conn, known, new_first
    ):
        films = [*_FILM, *_NEW_FILM]
        seasons = [*_SHOW, _season("show", 1, prefix="new-")]
        if known:
            _sync(conn, films=films, seasons=seasons)

        _scan(
            conn,
            films=sorted(films, key=lambda f: f["plex_rating_key"], reverse=new_first),
            seasons=sorted(seasons, key=lambda s: s["plex_rating_key"], reverse=not new_first),
            watched={"f1": _YESTERDAY, "show-s1": _YESTERDAY},
        )

        assert _scheduled(conn) == set()

    def test_a_watch_of_the_old_key_in_a_library_scanned_later_counts(self, conn):
        plex = _Plex(watched={"f1": _YESTERDAY})
        listed = {"1": _NEW_FILM, "3": _FILM}
        plex.get_movie_items = lambda library: listed[library]

        _engine(conn, plex, library_types={"1": "movie", "3": "movie"}).run_scan()

        assert _scheduled(conn) == set()

    def test_a_watch_of_the_old_key_cancels_no_deletion_already_scheduled(self, conn):
        _scan(conn, films=[*_FILM, *_NEW_FILM])

        _scan(conn, films=[*_FILM, *_NEW_FILM], watched={"f1": _YESTERDAY})

        assert _scheduled(conn) == {"f1", "new-f1"}

    @pytest.mark.parametrize("empty_lists", [0, 2])
    def test_an_earlier_watch_never_moves_a_later_one_back(self, conn, freezer, empty_lists):
        _sync(conn, films=[*_FILM, *_NEW_FILM])
        _set_watched(conn, "f1", _LONG_AGO)
        _set_watched(conn, "new-f1", _YESTERDAY)
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        _sync(conn, films=_NEW_FILM)

        assert _watched(conn, "new-f1") == _YESTERDAY.isoformat()

    def test_a_title_last_watched_long_ago_is_still_scheduled(self, conn, freezer):
        _sync(conn, films=_FILM)
        _set_watched(conn, "f1", _LONG_AGO)
        freezer.tick(60)

        _scan(conn, films=_NEW_FILM)

        assert _scheduled(conn) == {"new-f1"}

    @pytest.mark.parametrize("empty_lists", [0, 2])
    @pytest.mark.parametrize(
        "other",
        [
            {"season_number": 2},
            {"file_path": "/tv/elsewhere"},
            {"file_path": ""},
            {"show_title": "Another"},
            {"tmdb_id": 8},
            {"tvdb_id": 8},
        ],
    )
    def test_a_season_not_surely_the_one_watched_takes_nothing(
        self, conn, freezer, empty_lists, other
    ):
        old = _season("show", 1)
        if "file_path" in other and not other["file_path"]:
            old |= other
        old |= {key: 7 for key in other if key.endswith("_id")}
        _sync(conn, seasons=[old])
        _set_watched(conn, "show-s1", _YESTERDAY)
        for _ in range(empty_lists):
            freezer.tick(60)
            _sync(conn)

        freezer.tick(60)
        _scan(conn, seasons=[_season("show", 1, prefix="new-") | other])

        assert _scheduled(conn) == {"new-show-s1"}

    def test_a_show_renamed_with_its_ids_the_same_is_still_watched(self, conn, freezer):
        ids = {"tmdb_id": 7, "tvdb_id": 8}
        _sync(conn, seasons=[_season("show", 1) | ids])
        _set_watched(conn, "show-s1", _YESTERDAY)

        freezer.tick(60)
        renamed = _season("show", 1, prefix="new-") | ids | {"show_title": "Show (2020)"}
        _scan(conn, seasons=[renamed])

        assert _scheduled(conn) == set()

    def test_a_watch_no_setting_can_count_is_forgotten(self, conn, freezer):
        _sync(conn, films=_FILM)
        _set_watched(conn, "f1", _YESTERDAY)
        for days in (1, 1, 400):
            freezer.tick(timedelta(days=days))
            _sync(conn, films=[_film(f"x{n}") for n in range(6)])
            _sync(conn, films=[_film(f"x{n}") for n in range(5)])

        assert conn.execute("SELECT count(*) FROM pruned_watches").fetchone()[0] == 0


def test_looking_for_an_earlier_watch_does_not_read_the_library(conn):
    queries = []
    conn.set_trace_callback(queries.append)
    carry_last_watched(conn, "f1")
    conn.set_trace_callback(None)

    plan = [
        row["detail"]
        for query in queries
        if query.startswith("SELECT")
        for row in conn.execute(f"EXPLAIN QUERY PLAN {query}")
    ]
    assert plan
    assert not [s for s in plan if s.startswith("SCAN") and "subquery" not in s.lower()]


def test_the_library_keeps_how_many_episodes_a_season_has(conn):
    _sync(
        conn,
        films=_FILM,
        seasons=[*_SHOW, _season("empty", 1, episodes=()), _season("show", 2, episodes=(1, 2, 3))],
    )

    counts = conn.execute("SELECT id, episode_count FROM media_items").fetchall()
    assert {r["id"]: r["episode_count"] for r in counts} == {
        "f1": None,
        "show-s1": 2,
        "empty-s1": None,
        "show-s2": 3,
    }
