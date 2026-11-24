"""Tests that a kept film or season keeps its files when Plex lists a new key beside the kept one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.core.audit import log_audit
from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.web.repository.library_api import apply_keep_in_tx
from mediaman.web.routes import kept as kept_routes
from tests.helpers.factories import insert_kept_show, insert_scheduled_action

_NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


class _Library:
    """A Plex with five other films, where *kind* "film" or "season" is listed as *keys* say."""

    def __init__(self, conn, root, kind):
        self._conn, self._root, self.kind = conn, root, kind
        self.ids = {}
        self.elsewhere = set()
        self.places = {}
        self.names = {}
        self.watched = {}
        self.failing = set()

    def file(self, key):
        name = "film.mkv" if self.kind == "film" else "E1.mkv"
        return self.folder(key) / self.names.get(key, name)

    def folder(self, key):
        return self._root / self.places.get(key, "Title")

    def _item(self, key):
        file = self.file(key)
        item = {
            "plex_rating_key": key,
            "title": "Title",
            "added_at": datetime(2020, 1, 1, tzinfo=UTC),
            "file_path": str(file if self.kind == "film" else file.parent),
            "file_size_bytes": 4,
            "tracked_files": [{"path": str(file), "size": 4}],
        }
        if self.kind == "season":
            item |= {"show_title": "Title", "season_number": 1, "show_rating_key": f"show-{key}"}
        return item | self.ids.get(key, {})

    def _history(self, key):
        if key in self.failing:
            raise requests.ConnectionError
        return [{"viewed_at": self.watched[key]}] if key in self.watched else []

    def _run(self, run, keys, **options):
        plex = MagicMock()
        items = [self._item(key) for key in keys]
        others = [
            {
                "plex_rating_key": f"x{n}",
                "title": "x",
                "added_at": _NOW + timedelta(days=3650),
                "file_path": f"/elsewhere/{n}.mkv",
                "file_size_bytes": 1,
            }
            for n in range(5)
        ]
        films = {
            "1": others + [i for i in items if i["plex_rating_key"] not in self.elsewhere],
            "3": [i for i in items if i["plex_rating_key"] in self.elsewhere],
        }
        plex.get_movie_items.side_effect = (
            films.get if self.kind == "film" else {"1": others, "3": []}.get
        )
        plex.get_show_seasons.return_value = items if self.kind == "season" else []
        plex.get_watch_history.side_effect = self._history
        plex.get_season_watch_history.side_effect = self._history
        engine = ScanEngine(
            conn=self._conn,
            plex_client=plex,
            library_ids=["1", "2", "3"],
            library_types={"1": "movie", "2": "show", "3": "movie"},
            secret_key="0123456789abcdef" * 4,
            **options,
        )
        getattr(engine, run)()

    def sync(self, *keys):
        # As the app builds a sync's engine, whatever the person's own dry-run setting.
        self._run("sync_library", keys, dry_run=True)

    def scan(self, *keys, **options):
        self._run("run_scan", keys, **options)

    def delete(self):
        self._run("execute_deletions", ())


@pytest.fixture
def conn(db_path, monkeypatch, freezer):
    freezer.move_to(_NOW)
    monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
    monkeypatch.setattr("mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None)
    return init_db(str(db_path))


@pytest.fixture(params=["film", "season"])
def library(request, conn, tmp_path, monkeypatch):
    root = tmp_path / "media"
    for folder in ("Title", "Another"):
        (root / folder).mkdir(parents=True)
        for name in ("film.mkv", "E1.mkv"):
            (root / folder / name).write_bytes(b"data")
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) "
        "VALUES ('plex_libraries', '[\"1\", \"2\", \"3\"]', '2026')"
    )
    conn.commit()
    return _Library(conn, root, request.param)


def _keep(conn, media_id, *, action="protected_forever", days=None):
    apply_keep_in_tx(
        conn,
        media_id=media_id,
        action=action,
        execute_at=days and (_NOW + timedelta(days=days)).isoformat(),
        now_iso=datetime.now(UTC).isoformat(),
        snooze_label="forever",
        new_token=f"tok-{media_id}",
        audit_detail="Kept",
        actor="admin",
    )


def _remove_keep(conn, media_id, monkeypatch):
    monkeypatch.setattr(kept_routes, "get_db", lambda: conn)
    assert kept_routes.api_unprotect(media_id, username="admin").status_code == 200


def _schedule(conn, media_id, **kwargs):
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        token=f"due-{media_id}",
        execute_at="2020-01-01",
        **{"delete_status": "pending", "token_used": False} | kwargs,
    )
    conn.commit()


def _actions(conn, media_id):
    return [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id = ? ORDER BY id", (media_id,)
        )
    ]


def _dry_run(conn):
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('dry_run', 'true', '2026')")
    conn.commit()


def _strand(conn, library):
    """Leave the keep of the old key where an older mediaman left it, beside the new key."""
    library.sync("old", "new")
    _keep(conn, "old")
    with conn:
        conn.execute(
            "INSERT INTO pruned_keeps (media_item_id, file_path, season_number, action, "
            "scheduled_at, token_used, snoozed_at) SELECT mi.id, mi.file_path, mi.season_number, "
            "sa.action, sa.scheduled_at, 0, sa.snoozed_at "
            "FROM media_items mi JOIN scheduled_actions sa ON sa.media_item_id = mi.id"
        )
        conn.execute("DELETE FROM scheduled_actions")
        conn.execute("DELETE FROM media_items WHERE id = 'old'")


class TestKeptFilesStay:
    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_through_the_old_key_going(self, conn, freezer, library, both):
        library.sync("old")
        _keep(conn, "old")

        library.scan(*both)
        library.scan("new")
        freezer.tick(timedelta(days=40))
        library.scan("new")
        library.scan("new")

        assert library.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]

    def test_when_the_new_key_was_already_due(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("new")

        assert library.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]

    def test_when_the_old_key_went_under_an_older_mediaman(self, conn, freezer, library):
        _strand(conn, library)
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("new")

        assert library.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]


@pytest.mark.parametrize("library", ["season"], indirect=True)
@pytest.mark.parametrize("beside", [True, False])
def test_a_kept_shows_files_stay_when_it_is_renamed_under_its_new_key(
    conn, freezer, library, beside
):
    library.ids = {"new": {"show_title": "Title (2020)"}}
    library.sync("old")
    insert_kept_show(
        conn,
        show_rating_key="show-old",
        show_title="Title",
        action="protected_forever",
        created_at=_NOW.isoformat(),
    )
    if beside:
        library.scan("old", "new")

    for days in (0, 1, 40, 1):
        freezer.tick(timedelta(days=days))
        library.scan("new")

    assert library.file("new").exists()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"tmdb_id": 1}, {"tmdb_id": 2}),
        ({"tvdb_id": 1}, {"tvdb_id": 2, "show_title": "Another"}),
        ({"tmdb_id": 1, "tracked_files": []}, {"tmdb_id": 2}),
    ],
)
def test_kept_files_stay_when_plex_corrects_the_match_under_a_new_key(
    conn, freezer, library, old, new
):
    library.ids = {"old": old, "new": new}
    library.sync("old")
    _keep(conn, "old")

    library.scan("old", "new")
    library.scan("new")
    for days in (40, 1):
        freezer.tick(timedelta(days=days))
        library.scan("new")

    assert library.file("new").exists()
    assert _actions(conn, "new") == ["protected_forever"]


def test_files_watched_lately_stay_when_the_watch_could_not_be_read_at_first(
    conn, freezer, library
):
    library.sync("old")
    library.failing = {"old"}
    library.scan("old", "new")
    library.failing, library.watched = set(), {"old": datetime.now(UTC)}
    library.scan("old", "new")

    library.scan("new")
    for days in (15, 1):
        freezer.tick(timedelta(days=days))
        library.scan("new")

    assert library.file("new").exists()
    assert _actions(conn, "new") == []


@pytest.mark.parametrize("gone", [0, 1])
def test_files_watched_lately_stay_when_plex_renames_them_under_a_new_key(
    conn, freezer, library, gone
):
    if gone and library.kind == "season":
        library.ids = {"new": {"show_title": "Another"}}
    else:
        library.ids = {"old": {"tmdb_id": 1}, "new": {"tmdb_id": 2}}
    library.watched = {"old": datetime.now(UTC)}
    library.sync("old")
    for _ in range(gone):
        freezer.tick(60)
        library.sync()

    library.scan("new")
    for days in (15, 1):
        freezer.tick(timedelta(days=days))
        library.scan("new")

    assert library.file("new").exists()


class TestKeyBesideAKeptOne:
    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_is_not_scheduled(self, conn, library, both):
        library.sync("old")
        _keep(conn, "old")

        library.scan(*both)

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize("library", ["season"], indirect=True)
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ({"season_number": 1}, {"season_number": 2}),
            ({"tmdb_id": 1}, {"tmdb_id": 2}),
            ({"tvdb_id": 1}, {"tvdb_id": 2}),
            ({"tmdb_id": 1, "tvdb_id": 3}, {"tmdb_id": 2, "tvdb_id": 3}),
            ({}, {"show_title": "Another"}),
        ],
    )
    def test_is_scheduled_and_given_no_keep_when_it_is_another_title_there(
        self, conn, freezer, library, old, new
    ):
        library.ids = {"old": old, "new": new}
        library.names = {"new": "Other E1.mkv"}
        library.file("new").write_bytes(b"data")
        library.sync("old")
        _keep(conn, "old")

        library.scan("old", "new")
        assert _actions(conn, "new") == ["scheduled_deletion"]
        freezer.tick(60)
        library.scan("new")
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_is_scheduled_and_given_no_keep_when_it_is_elsewhere(self, conn, freezer, library):
        library.places = {"new": "Another"}
        library.sync("old")
        _keep(conn, "old")

        library.scan("old", "new")
        assert _actions(conn, "new") == ["scheduled_deletion"]
        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_is_scheduled_once_the_keep_ends(self, conn, freezer, library):
        library.sync("old")
        _keep(conn, "old", action="snoozed", days=9)
        library.scan("old", "new")
        assert _actions(conn, "new") == []

        freezer.tick(timedelta(days=10))
        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]


class TestKeepGivenAsTheOldKeyGoes:
    @pytest.mark.parametrize("library", ["film", "season"], indirect=True)
    def test_a_shorter_one_is_not_added_to_one_forever(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old", action="snoozed", days=9)
        _keep(conn, "new")

        freezer.tick(60)
        library.sync("new")

        assert _actions(conn, "new") == ["protected_forever"]

    @pytest.mark.parametrize("own", [5, 20])
    def test_the_longer_of_two_stays(self, conn, freezer, library, own):
        library.sync("old", "new")
        _keep(conn, "old", action="snoozed", days=9)
        _keep(conn, "new", action="snoozed", days=own)

        freezer.tick(60)
        library.sync("new")

        rows = conn.execute("SELECT execute_at FROM scheduled_actions").fetchall()
        until = (_NOW + timedelta(days=max(own, 9))).isoformat()
        assert [r["execute_at"] for r in rows] == [until]

    def test_one_removed_from_the_new_key_is_not_given_again(
        self, conn, freezer, library, monkeypatch
    ):
        library.sync("old")
        _keep(conn, "old")
        library.sync("old", "new")
        _keep(conn, "new")
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)

        freezer.tick(60)
        library.scan("new")
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_one_removed_from_the_new_key_is_not_given_to_the_next(
        self, conn, freezer, library, monkeypatch
    ):
        library.sync("old", "new")
        _keep(conn, "old")
        _keep(conn, "new")
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)
        freezer.tick(60)
        library.sync("new")

        freezer.tick(60)
        library.sync("newer")

        assert _actions(conn, "newer") == []

    def test_one_made_after_the_new_key_lost_its_own_is_given(
        self, conn, freezer, library, monkeypatch
    ):
        library.sync("old", "new")
        _keep(conn, "new")
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)
        freezer.tick(60)
        _keep(conn, "old")

        freezer.tick(60)
        library.sync("new")

        assert _actions(conn, "new") == ["protected_forever"]

    def test_an_undone_keep_of_the_new_key_does_not_count_as_removed(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)
        with conn:
            log_audit(conn, "new", "unprotected", "Keep undone by amy", actor="amy")

        library.sync("new")

        assert _actions(conn, "new") == ["protected_forever"]

    def test_a_deletion_being_carried_out_keeps_its_row_for_the_pass(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        _schedule(conn, "new", delete_status="deleting")

        freezer.tick(60)
        library.sync("new")

        assert _actions(conn, "new") == ["scheduled_deletion", "protected_forever"]

    def test_what_the_cancelled_deletion_removed_is_booked(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        _schedule(conn, "new")
        with conn:
            conn.execute(
                "UPDATE scheduled_actions SET removed_files = ? WHERE media_item_id = 'new'",
                ('{"paths": ["/gone.mkv"], "bytes": 4}',),
            )

        freezer.tick(60)
        library.sync("new")

        booked = conn.execute(
            "SELECT space_reclaimed_bytes FROM audit_log WHERE action = 'deleted'"
        ).fetchall()
        assert [r[0] for r in booked] == [4]

    def test_the_old_key_is_kept_again_if_it_comes_back(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync("new")

        library.sync("old", "new")

        assert _actions(conn, "old") == ["protected_forever"]


class TestKeepStrandedByAnOlderMediaman:
    def test_one_removed_since_stays_removed(self, conn, freezer, library, monkeypatch):
        _strand(conn, library)
        freezer.tick(60)
        _keep(conn, "new")
        _remove_keep(conn, "new", monkeypatch)

        library.sync("new")

        assert _actions(conn, "new") == []

    def test_one_given_is_not_given_again(self, conn, freezer, library):
        _strand(conn, library)
        library.sync("new")
        with conn:
            conn.execute("DELETE FROM scheduled_actions")

        library.sync("new")

        assert _actions(conn, "new") == []

    def test_it_still_goes_to_a_new_key_too(self, conn, freezer, library):
        _strand(conn, library)
        library.sync("new")
        freezer.tick(60)
        library.sync("new")

        library.sync("new", "newer")

        assert _actions(conn, "newer") == ["protected_forever"]

    def test_another_season_is_not_kept(self, conn, freezer, library):
        _strand(conn, library)
        with conn:
            conn.execute("UPDATE media_items SET season_number = 2 WHERE id = 'new'")

        library.sync("new")

        assert _actions(conn, "new") == []


_FILM = pytest.mark.parametrize("library", ["film"], indirect=True)


@pytest.fixture
def two_files(library):
    """The film's other key has a file of its own, of the same match."""
    library.ids = {key: {"tmdb_id": 7} for key in ("old", "new", "newer")}
    library.names = {"new": "film 4K.mkv", "newer": "film remux.mkv"}
    for key in ("new", "newer"):
        library.file(key).write_bytes(b"data")
    return library


def _until(conn, media_id):
    return [
        r["execute_at"]
        for r in conn.execute(
            "SELECT execute_at FROM scheduled_actions WHERE media_item_id = ?", (media_id,)
        )
    ]


@_FILM
class TestFilmReplacedByAnotherFile:
    @pytest.mark.parametrize(
        "lists", [[], [["old", "new"]], [["new", "old"]], [[]], [[], []]], ids=str
    )
    @pytest.mark.parametrize("run", ["sync", "scan"])
    def test_it_is_kept_under_its_new_key(self, conn, freezer, two_files, lists, run):
        two_files.sync("old")
        _keep(conn, "old")
        for keys in lists:
            freezer.tick(60)
            two_files.sync(*keys)
        two_files.file("old").unlink()

        for days in (0, 1, 40, 1):
            freezer.tick(timedelta(days=days, seconds=60))
            getattr(two_files, run)("new")
        two_files.scan("new")

        assert two_files.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]

    @pytest.mark.parametrize("gone", [0, 2])
    def test_a_keep_for_a_while_ends_when_it_did(self, conn, freezer, two_files, gone):
        two_files.sync("old")
        _keep(conn, "old", action="snoozed", days=9)
        for _ in range(gone):
            freezer.tick(60)
            two_files.sync()

        freezer.tick(60)
        two_files.scan("new")

        assert _until(conn, "new") == [(_NOW + timedelta(days=9)).isoformat()]
        freezer.tick(timedelta(days=10))
        two_files.scan("new")
        assert "scheduled_deletion" in _actions(conn, "new")

    def test_a_keep_that_ended_meanwhile_is_not_given(self, conn, freezer, two_files):
        two_files.sync("old")
        _keep(conn, "old", action="snoozed", days=9)
        freezer.tick(60)
        two_files.sync()
        freezer.tick(timedelta(days=10))

        two_files.sync("new")

        assert _actions(conn, "new") == []

    def test_it_is_kept_in_a_dry_run_too(self, conn, freezer, two_files):
        _dry_run(conn)
        two_files.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        two_files.sync()

        two_files.scan("new", dry_run=True)

        assert _actions(conn, "new") == ["protected_forever"]

    def test_it_is_kept_though_another_copy_went_with_the_kept_one(self, conn, freezer, two_files):
        two_files.sync("old", "new")
        _keep(conn, "old")
        for keys in ([], ["newer"]):
            freezer.tick(60)
            two_files.sync(*keys)

        assert _actions(conn, "newer") == ["protected_forever"]

    @pytest.mark.parametrize("lists", [[], [["new"]], [["new"], ["new"]]], ids=str)
    def test_it_is_kept_though_another_copy_lost_its_own_keep_to_a_person(
        self, conn, freezer, two_files, monkeypatch, lists
    ):
        two_files.sync("old", "new")
        _keep(conn, "old")
        _keep(conn, "new")
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)
        for keys in [*lists, ["new", "newer"]]:
            freezer.tick(60)
            two_files.sync(*keys)

        freezer.tick(60)
        two_files.scan("new", "newer")

        assert _actions(conn, "newer") == ["protected_forever"]
        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_an_expired_keep_of_another_copy_does_not_stop_it(self, conn, freezer, two_files):
        two_files.sync("old", "new")
        _keep(conn, "old")
        _keep(conn, "new", action="snoozed", days=1)
        freezer.tick(timedelta(days=2))

        two_files.sync("new")

        assert "protected_forever" in _actions(conn, "new")

    @pytest.mark.parametrize("loses", ["old", "new"])
    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    def test_it_is_kept_though_plex_lost_a_match_meanwhile(
        self, conn, freezer, two_files, loses, lists
    ):
        two_files.sync("old")
        _keep(conn, "old")
        matched = two_files.ids
        if loses == "old":
            freezer.tick(60)
            two_files.ids = matched | {"old": {"tmdb_id": None}}
            two_files.sync("old")
            two_files.sync("old")
        for keys in lists:
            freezer.tick(60)
            two_files.scan(*keys)
            assert _actions(conn, "new") == []
        if loses == "new":
            freezer.tick(60)
            two_files.sync("new", "old")
            two_files.ids = matched | {"new": {"tmdb_id": None}}
            two_files.scan("new", "old")
            assert _actions(conn, "new") == []

        freezer.tick(60)
        two_files.scan("new")

        assert _actions(conn, "new") == ["protected_forever"]

    def test_it_is_kept_though_plex_lost_the_match_a_database_from_before_held(
        self, conn, freezer, two_files
    ):
        two_files.sync("old")
        _keep(conn, "old")
        with conn:
            conn.execute("UPDATE media_items SET last_tmdb_id = NULL")
        matched = two_files.ids
        two_files.ids = matched | {"old": {"tmdb_id": None}}
        freezer.tick(60)
        two_files.sync("old")

        freezer.tick(60)
        two_files.sync("new")

        assert _actions(conn, "new") == ["protected_forever"]

    @pytest.mark.parametrize("lost", [7, None])
    def test_the_match_plex_corrected_to_counts_and_not_the_one_before(
        self, conn, freezer, two_files, lost
    ):
        two_files.ids["old"] = {"tmdb_id": 8}
        two_files.sync("old")
        for tmdb_id in (7, lost):
            two_files.ids["old"] = {"tmdb_id": tmdb_id}
            two_files.sync("old")
        _keep(conn, "old")
        two_files.ids["newer"] = {"tmdb_id": 8}

        freezer.tick(60)
        two_files.scan("new", "newer")

        assert _actions(conn, "new") == ["protected_forever"]
        assert _actions(conn, "newer") == ["scheduled_deletion"]

    def test_a_film_plex_has_not_matched_again_is_not_given_its_last_match(
        self, conn, freezer, two_files
    ):
        two_files.sync("old")
        two_files.ids["old"] = {"tmdb_id": None}

        two_files.sync("old")

        row = conn.execute("SELECT tmdb_id FROM media_items WHERE id = 'old'").fetchone()
        assert row["tmdb_id"] is None

    def test_it_is_kept_once_plex_matches_it(self, conn, freezer, two_files):
        matched, two_files.ids["new"] = two_files.ids["new"], {}
        two_files.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        two_files.scan("new")
        assert _actions(conn, "new") == ["scheduled_deletion"]

        two_files.ids["new"] = matched
        freezer.tick(60)
        two_files.sync("new")

        assert _actions(conn, "new") == ["protected_forever"]

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @pytest.mark.parametrize("other", ["id", "library", "unmatched"])
    def test_another_title_is_not_kept(self, conn, freezer, two_files, lists, other):
        if other == "library":
            two_files.elsewhere = {"new"}
        else:
            two_files.ids["new"] = {"tmdb_id": 8 if other == "id" else None}
        two_files.sync("old")
        _keep(conn, "old")
        for keys in lists:
            freezer.tick(60)
            two_files.scan(*keys)
            if "new" in keys:
                assert _actions(conn, "new") == ["scheduled_deletion"]

        freezer.tick(60)
        two_files.scan("new")
        two_files.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    def test_a_keep_removed_from_the_new_key_is_not_given_to_the_next(
        self, conn, freezer, two_files, monkeypatch, lists
    ):
        two_files.sync("old")
        _keep(conn, "old")
        for keys in [*lists, ["new"]]:
            freezer.tick(60)
            two_files.scan(*keys)
        _remove_keep(conn, "new", monkeypatch)

        freezer.tick(60)
        two_files.scan("new", "newer")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        assert _actions(conn, "newer") == ["scheduled_deletion"]

    def test_the_old_key_is_kept_again_if_it_comes_back(self, conn, freezer, two_files):
        two_files.sync("old")
        _keep(conn, "old")
        for keys in ([], ["new"], ["old", "new"]):
            freezer.tick(60)
            two_files.sync(*keys)

        assert _actions(conn, "old") == ["protected_forever"]

    def test_the_old_path_is_kept_under_a_key_listed_later(self, conn, freezer, two_files):
        two_files.sync("old")
        _keep(conn, "old")
        two_files.names["newer"] = two_files.file("old").name
        for keys in ([], ["new"], ["new", "newer"]):
            freezer.tick(60)
            two_files.sync(*keys)

        assert _actions(conn, "newer") == ["protected_forever"]

    def test_a_keep_set_aside_by_an_older_mediaman_waits_for_its_path(
        self, conn, freezer, two_files
    ):
        two_files.sync("old")
        with conn:
            conn.execute(
                "INSERT INTO pruned_keeps (media_item_id, file_path, season_number, action, "
                "scheduled_at, execute_at, token_used, snoozed_at, snooze_duration) "
                "VALUES ('gone', ?, NULL, 'protected_forever', '2026', NULL, 0, NULL, NULL)",
                (str(two_files.file("newer")),),
            )

        two_files.sync("old", "new")
        assert _actions(conn, "new") == []

        two_files.sync("old", "new", "newer")
        assert _actions(conn, "newer") == ["protected_forever"]


@_FILM
class TestOtherCopyOfAKeptFilm:
    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_is_not_scheduled_and_given_no_keep(self, conn, two_files, both):
        two_files.sync("old")
        _keep(conn, "old")

        two_files.scan(*both)

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize("run", ["sync", "scan"])
    def test_does_not_stay_scheduled(self, conn, freezer, two_files, run):
        two_files.scan("old", "new")
        assert _actions(conn, "new") == ["scheduled_deletion"]
        freezer.tick(60)
        _keep(conn, "old")

        getattr(two_files, run)("old", "new")

        assert _actions(conn, "new") == []

    def test_that_is_due_is_not_deleted(self, conn, freezer, two_files):
        two_files.sync("old", "new")
        _schedule(conn, "new")
        _keep(conn, "old")

        two_files.delete()

        assert two_files.file("new").exists()
        assert _actions(conn, "new") == []

    def test_that_is_due_is_not_deleted_before_a_database_from_before_is_synced(
        self, conn, two_files
    ):
        two_files.sync("old", "new")
        with conn:
            conn.execute("UPDATE media_items SET last_tmdb_id = NULL")
        _schedule(conn, "new")
        _keep(conn, "old")

        two_files.delete()

        assert two_files.file("new").exists()
        assert _actions(conn, "new") == []

    def test_that_took_its_keep_uses_it_up_though_another_was_refused_it(
        self, conn, freezer, two_files, monkeypatch
    ):
        two_files.ids["newest"] = {"tmdb_id": 7}
        two_files.names["newest"] = "film 8K.mkv"
        two_files.sync("old", "new", "newer")
        for key in ("old", "new"):
            _keep(conn, key)
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)
        two_files.sync("new", "newer")
        assert _actions(conn, "newer") == ["protected_forever"]
        freezer.tick(60)
        _remove_keep(conn, "newer", monkeypatch)

        two_files.sync("new", "newer", "newest")

        assert _actions(conn, "newest") == []

    def test_that_lost_its_own_keep_to_a_person_is_scheduled(
        self, conn, freezer, two_files, monkeypatch
    ):
        two_files.sync("old", "new")
        _keep(conn, "old")
        _keep(conn, "new")
        freezer.tick(60)
        _remove_keep(conn, "new", monkeypatch)

        two_files.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_is_scheduled_once_the_keep_ends(self, conn, freezer, two_files):
        two_files.sync("old", "new")
        _keep(conn, "old", action="snoozed", days=9)
        freezer.tick(timedelta(days=10))

        two_files.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_has_its_keep_until_the_same_day_once_the_kept_one_is_gone(
        self, conn, freezer, two_files
    ):
        two_files.sync("old", "new")
        _keep(conn, "old", action="snoozed", days=9)
        two_files.file("old").unlink()
        freezer.tick(60)

        two_files.sync("new")

        assert _until(conn, "new") == [(_NOW + timedelta(days=9)).isoformat()]

    def test_at_the_kept_ones_path_under_a_new_key_is_kept_though_another_was_given_it(
        self, conn, freezer, two_files
    ):
        two_files.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)
        two_files.sync("new")
        two_files.names["newer"] = two_files.file("old").name

        two_files.sync("new", "newer")

        assert _actions(conn, "newer") == ["protected_forever"]


@pytest.mark.parametrize("run", ["sync", "scan"])
def test_a_key_of_the_kept_files_does_not_stay_scheduled(conn, freezer, library, run):
    library.scan("old", "new")
    freezer.tick(60)
    _keep(conn, "old")

    getattr(library, run)("old", "new")

    assert _actions(conn, "new") == []


@pytest.mark.parametrize("library", ["season"], indirect=True)
class TestOtherKeyOfAKeptShow:
    @staticmethod
    def _keep_show(conn, **kwargs):
        insert_kept_show(
            conn,
            show_rating_key="show-old",
            show_title="Title",
            created_at=datetime.now(UTC).isoformat(),
            **{"action": "protected_forever"} | kwargs,
        )

    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_is_not_scheduled(self, conn, library, both):
        library.sync("old")
        self._keep_show(conn)

        library.scan(*both)

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize("run", ["sync", "scan"])
    def test_does_not_stay_scheduled(self, conn, freezer, library, run):
        library.scan("old", "new")
        freezer.tick(60)
        self._keep_show(conn)

        getattr(library, run)("old", "new")

        assert _actions(conn, "new") == []

    def test_that_is_due_is_not_left_waiting(self, conn, library):
        library.sync("old", "new")
        _schedule(conn, "new")
        self._keep_show(conn)

        library.delete()

        assert library.file("new").exists()
        assert _actions(conn, "new") == []

    def test_that_is_due_stays_so_once_the_keep_ended(self, conn, freezer, library):
        library.sync("old", "new")
        _schedule(conn, "new")
        self._keep_show(conn, action="snoozed", execute_at=_NOW.isoformat())
        freezer.tick(60)

        library.delete()

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_is_scheduled_when_it_is_another_show_there(self, conn, library):
        library.ids = {"old": {"tvdb_id": 1}, "new": {"tvdb_id": 2}}
        library.names = {"new": "Other E1.mkv"}
        library.file("new").write_bytes(b"data")
        library.sync("old")
        self._keep_show(conn)

        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]


def test_a_deletion_of_a_key_of_the_kept_files_being_carried_out_is_left_to_the_pass(conn, library):
    library.sync("old", "new")
    _schedule(conn, "new", delete_status="deleting")
    _keep(conn, "old")

    library.sync("old", "new")

    assert _actions(conn, "new") == ["scheduled_deletion"]
    refused = conn.execute("SELECT 1 FROM audit_log WHERE action = 'delete_refused'")
    assert refused.fetchall() == []


@pytest.mark.parametrize("run", ["sync", "scan"])
def test_a_dry_run_cancels_no_deletion_of_a_key_of_the_kept_files(conn, library, run):
    _dry_run(conn)
    library.sync("old", "new")
    _schedule(conn, "new")
    _keep(conn, "old")

    if run == "sync":
        library.sync("old", "new")
    else:
        library.scan("old", "new", dry_run=True)

    assert _actions(conn, "new") == ["scheduled_deletion"]


def test_a_used_deletion_of_a_key_of_the_kept_files_is_left(conn, library):
    library.sync("old", "new")
    _schedule(conn, "new", token_used=True)
    _keep(conn, "old")

    library.sync("old", "new")

    assert _actions(conn, "new") == ["scheduled_deletion"]


def test_a_cancelled_deletion_of_a_key_of_the_kept_files_is_in_the_history(conn, library):
    library.sync("old", "new")
    _schedule(conn, "new")
    _keep(conn, "old")

    library.sync("old", "new")
    library.sync("old", "new")

    refused = conn.execute("SELECT media_item_id FROM audit_log WHERE action = 'delete_refused'")
    assert [r["media_item_id"] for r in refused] == ["new"]


def test_what_a_cancelled_deletion_already_removed_is_booked(conn, freezer, library):
    library.sync("old", "new")
    _schedule(conn, "new")
    with conn:
        conn.execute(
            "UPDATE scheduled_actions SET removed_files = ?",
            ('{"paths": ["/gone.mkv"], "bytes": 4}',),
        )
    _keep(conn, "old")

    library.sync("old", "new")

    booked = conn.execute("SELECT space_reclaimed_bytes FROM audit_log WHERE action = 'deleted'")
    assert [r[0] for r in booked] == [4]


@pytest.mark.parametrize("library", ["season"], indirect=True)
@pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
def test_another_season_of_the_show_of_a_kept_one_is_not_kept(conn, freezer, library, lists):
    library.ids = {
        "old": {"tmdb_id": 7, "season_number": 1},
        "new": {"tmdb_id": 7, "season_number": 2},
    }
    library.places = {"new": "Another"}
    library.sync("old")
    _keep(conn, "old")
    for keys in lists:
        freezer.tick(60)
        library.scan(*keys)
        assert _actions(conn, "new") == (["scheduled_deletion"] if "new" in keys else [])

    freezer.tick(60)
    library.scan("new")
    library.scan("new")

    assert _actions(conn, "new") == ["scheduled_deletion"]
