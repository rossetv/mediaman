"""Tests that keeps follow a title to a new key at a new path, found by its ids."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from mediaman.core.audit import log_audit
from mediaman.db import init_db
from mediaman.scanner.repository import rekeyed
from tests.helpers.factories import insert_kept_show
from tests.unit.scanner.test_keep_beside_new_key import (
    _NOW,
    _actions,
    _keep,
    _Library,
    _remove_keep,
    _schedule,
)


@pytest.fixture
def conn(db_path, monkeypatch, freezer):
    freezer.move_to(_NOW)
    monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
    monkeypatch.setattr("mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None)
    return init_db(str(db_path))


@pytest.fixture(params=["film", "season"])
def library(request, conn, tmp_path, monkeypatch):
    """A title whose keys each have a path of their own, with the ids of one match."""
    root = tmp_path / "media"
    kind, _, where = request.param.partition(" ")
    library = _Library(conn, root, kind)
    library.places = {key: key for key in ("old", "new", "newer")}
    for key in library.places:
        library.folder(key).mkdir(parents=True)
        library.file(key).write_bytes(b"data")
        library.ids[key] = {"tmdb_id": 7} if kind == "film" else {"tmdb_id": 7, "tvdb_id": 8}
    if where:
        library.elsewhere = {where}
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) "
            'VALUES (\'plex_libraries\', \'["1", "2", "3", "4"]\', \'2026\')'
        )
    return library


def _keep_show(conn, **kwargs):
    insert_kept_show(
        conn,
        show_rating_key="show-old",
        show_title="Title",
        created_at=_NOW.isoformat(),
        **{"action": "protected_forever"} | kwargs,
    )


def _keep_either(conn, library, kept):
    if kept == "show":
        _keep_show(conn)
    else:
        _keep(conn, "old")


def _is_kept(conn, kept, key):
    if kept == "show":
        row = conn.execute("SELECT 1 FROM kept_shows WHERE show_rating_key = ?", (f"show-{key}",))
        return row.fetchone() is not None
    return "protected_forever" in _actions(conn, key)


def _season(*params):
    return pytest.mark.parametrize("library", params or ["season"], indirect=True)


# The key in the second library of its kind, which is read last, if any.
_MOVES = ["season", "season new", "season old"]
_FILM_MOVES = ["film", "film new", "film old"]
_SEQUENCES = pytest.mark.parametrize(
    "lists", [[], [["old", "new"]], [["new", "old"]], [[]], [[], []]], ids=str
)
_EVERY_KEEP = pytest.mark.parametrize(
    ("library", "kept"),
    [("season", "season"), ("season", "show"), ("film", "film")],
    indirect=["library"],
)
_KEPT = pytest.mark.parametrize("kept", ["season", "show"])


def _run_until_settled(conn, freezer, library, run, key="new"):
    for days in (0, 1, 40, 1):
        freezer.tick(timedelta(days=days, seconds=60))
        getattr(library, run)(key)
        assert "scheduled_deletion" not in _actions(conn, key)
    library.scan(key)


class TestKeptTitleMoves:
    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_SEQUENCES
    @_KEPT
    @_season(*_MOVES)
    def test_a_kept_season_or_show_is_kept_under_its_new_key_at_its_new_path(
        self, conn, freezer, library, kept, lists, run
    ):
        library.sync("old")
        _keep_either(conn, library, kept)
        for keys in lists:
            freezer.tick(60)
            library.sync(*keys)
        library.file("old").unlink()

        _run_until_settled(conn, freezer, library, run)

        assert library.file("new").exists()
        assert _is_kept(conn, kept, "new")

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_SEQUENCES
    @_season(*_FILM_MOVES)
    def test_a_kept_film_is_kept_in_the_library_it_moved_to(
        self, conn, freezer, library, lists, run
    ):
        library.sync("old")
        _keep(conn, "old")
        for keys in lists:
            freezer.tick(60)
            library.sync(*keys)
        library.file("old").unlink()

        _run_until_settled(conn, freezer, library, run)

        assert library.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]

    @_KEPT
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ({"tmdb_id": None}, {}),
            ({"tvdb_id": None}, {}),
            ({}, {"tvdb_id": None}),
            ({"tmdb_id": 6}, {}),
            ({}, {"show_title": "Title (2020)"}),
        ],
    )
    @_season()
    def test_the_tvdb_id_tells_the_show_and_else_the_tmdb_id(
        self, conn, freezer, library, kept, old, new
    ):
        library.ids["old"] |= old
        library.ids["new"] |= new
        library.sync("old")
        _keep_either(conn, library, kept)
        freezer.tick(60)
        library.sync()

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") in ([], ["protected_forever"])
        assert _is_kept(conn, kept, "new")

    @_season()
    def test_a_match_plex_lost_still_tells_the_season(self, conn, freezer, library):
        library.sync("old")
        _keep(conn, "old")
        library.ids["old"] = {}
        for keys in (["old"], []):
            freezer.tick(60)
            library.sync(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["protected_forever"]

    @_KEPT
    @_season()
    def test_a_season_plex_matches_later_is_kept_then(self, conn, freezer, library, kept):
        ids, library.ids["new"] = library.ids["new"], {}
        library.sync("old")
        _keep_either(conn, library, kept)
        freezer.tick(60)
        library.scan("new")
        assert _actions(conn, "new") == ["scheduled_deletion"]

        library.ids["new"] = ids
        freezer.tick(60)
        library.sync("new")

        assert "scheduled_deletion" not in _actions(conn, "new")
        assert _is_kept(conn, kept, "new")

    @pytest.mark.parametrize("kind", ["item", "show"])
    @_season("season", "season new")
    def test_a_keep_for_a_while_ends_when_it_did(self, conn, freezer, library, kind):
        until = (_NOW + timedelta(days=9)).isoformat()
        library.sync("old")
        if kind == "show":
            _keep_show(conn, action="snoozed", execute_at=until)
        else:
            _keep(conn, "old", action="snoozed", days=9)
        for keys in ([], ["new"]):
            freezer.tick(60)
            library.sync(*keys)

        rows = conn.execute(
            "SELECT execute_at FROM kept_shows WHERE show_rating_key = 'show-new' "
            "UNION ALL SELECT execute_at FROM scheduled_actions WHERE media_item_id = 'new'"
        ).fetchall()
        assert [r["execute_at"] for r in rows] == [until]
        freezer.tick(timedelta(days=10))
        library.scan("new")
        assert "scheduled_deletion" in _actions(conn, "new")

    @_season()
    def test_a_whole_show_keep_follows_the_show_though_its_new_key_lists_another_season(
        self, conn, freezer, library
    ):
        library.ids["new"]["season_number"] = 2
        library.sync("old")
        _keep_show(conn)
        freezer.tick(60)
        library.sync()

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == []
        assert _is_kept(conn, "show", "new")

    @_season()
    def test_a_show_with_no_path_is_still_found_by_its_ids(self, conn, freezer, library):
        for key in ("old", "newer"):
            library.ids[key]["file_path"] = ""
        library.ids["newer"] |= {"tmdb_id": 17, "tvdb_id": 18}
        library.sync("old", "newer")
        _keep_show(conn)
        insert_kept_show(
            conn, show_rating_key="show-newer", show_title="Title", created_at=_NOW.isoformat()
        )
        for keys in (["newer"], ["newer"], ["newer", "new"]):
            freezer.tick(60)
            library.sync(*keys)

        assert _is_kept(conn, "show", "new")

    @_KEPT
    @_season(*_MOVES)
    def test_one_due_is_not_deleted_while_the_unlisted_key_is_not_removed(
        self, conn, freezer, library, kept
    ):
        library.sync("old", "new")
        _keep_either(conn, library, kept)
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("new", dry_run=True)
        library.delete()

        assert library.file("new").exists()

    @_season(*_MOVES, *_FILM_MOVES)
    def test_one_already_due_is_not_deleted(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("new")

        assert library.file("new").exists()
        assert _actions(conn, "new") == ["protected_forever"]

    @_season(*_MOVES, *_FILM_MOVES)
    def test_the_old_key_is_kept_again_if_it_comes_back(self, conn, freezer, library):
        library.sync("old")
        _keep(conn, "old")
        for keys in ([], ["new"], ["old", "new"]):
            freezer.tick(60)
            library.sync(*keys)

        assert _actions(conn, "old") == ["protected_forever"]

    @_EVERY_KEEP
    def test_a_copy_in_another_library_does_not_use_the_keep_up(self, conn, freezer, library, kept):
        """Nothing tells it from the moved title, which may be listed only later."""
        library.elsewhere = {"newer"}
        library.sync("old", "newer")
        _keep_either(conn, library, kept)
        for keys in (["newer"], ["newer", "new"]):
            freezer.tick(60)
            library.sync(*keys)

        assert _is_kept(conn, kept, "new")


class TestRemovedKeep:
    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_season("season", "film new", "season new")
    def test_one_removed_from_the_new_key_is_not_given_to_the_next(
        self, conn, freezer, library, monkeypatch, lists
    ):
        if library.elsewhere:
            library.elsewhere.add("newer")
        library.sync("old")
        _keep(conn, "old")
        for keys in [*lists, ["new"]]:
            freezer.tick(60)
            library.scan(*keys)
        assert _actions(conn, "new") == ["protected_forever"]
        _remove_keep(conn, "new", monkeypatch)

        freezer.tick(60)
        library.scan("new", "newer")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        assert _actions(conn, "newer") == ["scheduled_deletion"]

    @_season("season", "season new")
    def test_a_whole_show_keep_removed_from_the_new_key_is_not_given_to_the_next(
        self, conn, freezer, library
    ):
        if library.elsewhere:
            library.elsewhere.add("newer")
        library.sync("old")
        _keep_show(conn)
        freezer.tick(60)
        library.scan("new")
        with conn:
            conn.execute("DELETE FROM kept_shows WHERE show_rating_key = 'show-new'")
            log_audit(conn, "show-new", "removed_show_keep", "removed", actor="admin")

        freezer.tick(60)
        library.scan("new", "newer")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        assert _actions(conn, "newer") == ["scheduled_deletion"]

    @_season("season")
    def test_nobody_can_remove_it_while_it_is_given(
        self, conn, db_path, freezer, library, monkeypatch
    ):
        library.ids["new"] = {}
        library.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync("new")
        with conn:
            conn.execute("UPDATE media_items SET tvdb_id = 8 WHERE id = 'new'")
        other = sqlite3.connect(db_path, timeout=0)
        locked = []

        def asked(*_args):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("DELETE FROM scheduled_actions")
            locked.append(True)
            return False

        monkeypatch.setattr(rekeyed, "_keep_removed_since", asked)

        rekeyed.carry_film_keeps(conn)

        assert locked


class TestAnotherTitle:
    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_KEPT
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param({}, {"tmdb_id": 17, "tvdb_id": 18}, id="another_show"),
            pytest.param({}, {"tvdb_id": 18}, id="another_tvdb_id"),
            pytest.param({}, {"tmdb_id": None, "tvdb_id": None}, id="no_ids"),
            pytest.param({"tmdb_id": None}, {"tvdb_id": None}, id="no_id_in_common"),
            pytest.param({"tmdb_id": 8, "tvdb_id": None}, {"tmdb_id": None}, id="ids_of_two_kinds"),
            *(
                pytest.param(empty, empty, id=f"both_{name}")
                for name, empty in {
                    "none": {"tmdb_id": None, "tvdb_id": None},
                    "zero": {"tmdb_id": 0, "tvdb_id": 0},
                    "empty": {"tmdb_id": "", "tvdb_id": ""},
                }.items()
            ),
        ],
    )
    @_season()
    def test_is_scheduled_and_given_no_keep(self, conn, freezer, library, kept, old, new, lists):
        library.ids["old"] |= old
        library.ids["new"] |= new
        library.sync("old")
        _keep_either(conn, library, kept)
        for keys in lists:
            freezer.tick(60)
            library.scan(*keys)

        freezer.tick(60)
        library.scan("new")
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        assert not _is_kept(conn, kept, "new")

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @pytest.mark.parametrize("empty", [None, 0, ""])
    @_season("film", "film new")
    def test_films_without_an_id_are_not_one(self, conn, freezer, library, empty, lists):
        for key in ("old", "new"):
            library.ids[key] = {"tmdb_id": empty}
        library.sync("old")
        _keep(conn, "old")
        for keys in lists:
            freezer.tick(60)
            library.scan(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_season("season", "season new")
    def test_another_season_of_the_show_of_a_kept_one_is_scheduled(
        self, conn, freezer, library, lists
    ):
        library.ids["new"]["season_number"] = 0
        library.sync("old")
        _keep(conn, "old")
        for keys in lists:
            freezer.tick(60)
            library.scan(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @_season("season")
    def test_a_film_with_the_tmdb_id_of_a_kept_seasons_show_is_scheduled(
        self, conn, freezer, library
    ):
        """TMDB numbers its films and its shows apart."""
        library.sync("old")
        _keep(conn, "old")
        _keep_show(conn)
        freezer.tick(60)
        library.sync()
        library.kind, library.ids["new"] = "film", {"tmdb_id": 7}

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @_season("season")
    def test_a_film_with_the_tmdb_id_of_the_show_of_a_kept_season_without_a_number_is_scheduled(
        self, conn, freezer, library
    ):
        library.ids["old"]["season_number"] = None
        library.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync()
        library.kind, library.ids["new"] = "film", {"tmdb_id": 7}

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @pytest.mark.parametrize("number", [1, None])
    @pytest.mark.parametrize("gone", [0, 1])
    @_season("film")
    def test_a_season_of_a_show_with_the_tmdb_id_of_a_kept_film_is_scheduled(
        self, conn, freezer, library, number, gone
    ):
        library.sync("old")
        _keep(conn, "old")
        for _ in range(gone):
            freezer.tick(60)
            library.sync()
        library.kind = "season"
        library.ids["new"]["season_number"] = number

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]


class TestBothListed:
    """A copy Plex lists beside the kept one is kept with it in its library, and not in another."""

    @_KEPT
    @_season()
    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_in_one_library_is_not_scheduled_and_given_no_keep(
        self, conn, freezer, library, kept, both
    ):
        library.sync("old")
        _keep_either(conn, library, kept)
        for _ in range(2):
            freezer.tick(60)
            library.scan(*both)

        assert _actions(conn, "new") == []
        assert not _is_kept(conn, kept, "new")

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @pytest.mark.parametrize(
        ("library", "failing"),
        [
            *((f"{kind} {key}", set()) for kind in ("season", "film") for key in ("new", "old")),
            # Its watches are not known, but Plex lists it.
            *((f"{kind} new", {"old"}) for kind in ("season", "film")),
        ],
        indirect=["library"],
    )
    def test_in_another_library_is_scheduled_and_given_no_keep(
        self, conn, freezer, library, run, failing
    ):
        kept = ["season", "show"] if library.kind == "season" else ["film"]
        library.sync("old")
        _keep(conn, "old")
        if library.kind == "season":
            _keep_show(conn)
        library.failing = failing
        for _ in range(2):
            freezer.tick(60)
            getattr(library, run)("old", "new")
        freezer.tick(60)
        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        assert not any(_is_kept(conn, k, "new") for k in kept)

    @_season("season new", "film new")
    def test_in_another_library_is_deleted_when_due(self, conn, freezer, library):
        library.sync("old", "new")
        _keep(conn, "old")
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("old", "new")

        assert not library.file("new").exists()
        assert library.file("old").exists()

    @_season("season", "season new")
    def test_a_show_with_a_season_still_listed_gives_another_key_no_keep(
        self, conn, freezer, library
    ):
        library.ids["newer"]["show_rating_key"] = "show-old"
        library.ids["newer"]["season_number"] = 2
        library.sync("old", "newer")
        _keep_show(conn)

        for _ in range(2):
            freezer.tick(60)
            library.sync("newer", "new")

        assert not _is_kept(conn, "show", "new")

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_EVERY_KEEP
    def test_in_another_library_is_given_no_keep_that_one_of_its_own_library_takes(
        self, conn, freezer, library, kept, run
    ):
        library.elsewhere = {"newer"}
        library.sync("old", "newer")
        _keep_either(conn, library, kept)
        for _ in range(2):
            freezer.tick(60)
            getattr(library, run)("new", "newer")

        assert _is_kept(conn, kept, "new")
        assert not _is_kept(conn, kept, "newer")
