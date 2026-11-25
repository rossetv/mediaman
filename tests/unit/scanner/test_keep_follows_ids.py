"""Tests that keeps and watches follow a title to a new key at a new path, found by its ids."""

from __future__ import annotations

import functools
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.audit import log_audit
from mediaman.db import init_db
from mediaman.scanner.phases import delete
from mediaman.scanner.repository import rekeyed
from mediaman.scanner.repository.watch_credit import carry_last_watched
from mediaman.web.repository.watch_progress import save_progress
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

    @_SEQUENCES
    @_KEPT
    @_season(*_MOVES)
    def test_a_tvdb_id_plex_lost_still_tells_a_show_that_never_had_another(
        self, conn, freezer, library, kept, lists
    ):
        library.ids["old"] = {"tvdb_id": 8}
        library.sync("old")
        library.ids["old"] = {}
        library.sync("old")
        library.sync("old")
        _keep_either(conn, library, kept)
        for keys in lists:
            freezer.tick(60)
            library.sync(*keys)

        freezer.tick(60)
        library.scan("new")

        assert "scheduled_deletion" not in _actions(conn, "new")
        assert _is_kept(conn, kept, "new")

    @_season()
    def test_a_tvdb_id_lost_beside_a_tmdb_id_does_not_hide_that_one(self, conn, freezer, library):
        """The TVDB id may be of the match before, so the TMDB id alone tells the season."""
        library.ids["old"] = {"tvdb_id": 18}
        library.sync("old")
        for ids in ({"tmdb_id": 7}, {}):
            library.ids["old"] = ids
            library.sync("old")
        _keep(conn, "old")

        library.scan("old", "new")
        assert _actions(conn, "new") == []
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
        self, conn, freezer, library, kept, not_removed
    ):
        library.sync("old", "new")
        _keep_either(conn, library, kept)
        _schedule(conn, "new")

        freezer.tick(60)
        library.scan("new")

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


@_season("season new")
class TestSeasonOfAKeptShowMoves:
    """One season goes to another library, under a new key of its show; another stays."""

    @pytest.fixture
    def show(self, conn, library):
        library.places["stays"] = "stays"
        library.folder("stays").mkdir()
        library.file("stays").write_bytes(b"data")
        library.ids["stays"] = library.ids["old"] | {
            "show_rating_key": "show-old",
            "season_number": 2,
        }
        return library

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @pytest.mark.parametrize("lists", [[], [["stays"]]], ids=str)
    def test_its_files_stay_and_its_new_show_gets_no_keep(self, conn, freezer, show, lists, run):
        show.sync("old", "stays")
        _keep_show(conn)
        for keys in lists:
            freezer.tick(60)
            show.sync(*keys)
        show.file("old").unlink()

        for days in (0, 1, 40, 1, 0):
            freezer.tick(timedelta(days=days, seconds=60))
            getattr(show, "scan" if not days else run)("stays", "new")
            assert "scheduled_deletion" not in _actions(conn, "new")

        assert show.file("new").exists()
        assert not _is_kept(conn, "show", "new")

    def test_one_already_due_is_not_deleted_once_plex_lists_the_old_one_no_more(
        self, conn, freezer, show, not_removed
    ):
        show.sync("old", "stays", "new")
        _keep_show(conn)
        _schedule(conn, "new")

        freezer.tick(60)
        show.scan("stays", "new")

        assert show.file("new").exists()

    def test_one_listed_beside_the_old_one_is_scheduled(self, conn, freezer, show):
        show.sync("old", "stays")
        _keep_show(conn)

        for _ in range(2):
            freezer.tick(60)
            show.scan("old", "stays", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_another_season_of_its_new_show_is_scheduled(self, conn, freezer, show):
        show.elsewhere.add("newer")
        show.ids["newer"] |= {"show_rating_key": "show-new", "season_number": 3}
        show.sync("old", "stays")
        _keep_show(conn)

        for _ in range(2):
            freezer.tick(60)
            show.scan("stays", "new", "newer")

        assert _actions(conn, "new") == []
        assert _actions(conn, "newer") == ["scheduled_deletion"]

    def test_it_is_scheduled_once_the_old_one_is_back_in_its_library(self, conn, freezer, show):
        show.sync("old", "stays")
        _keep_show(conn)
        freezer.tick(60)
        show.scan("stays", "new")
        show.ids["newer"] |= {"show_rating_key": "show-old"}

        for _ in range(2):
            freezer.tick(60)
            show.scan("newer", "stays", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_it_is_scheduled_once_the_shows_keep_is_removed(self, conn, freezer, show):
        show.sync("old", "stays")
        _keep_show(conn)
        freezer.tick(60)
        show.scan("stays", "new")
        with conn:
            conn.execute("DELETE FROM kept_shows")
            log_audit(conn, "show-old", "removed_show_keep", "removed", actor="admin")

        freezer.tick(60)
        show.scan("stays", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]


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

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @pytest.mark.parametrize("still_listed", [True, False])
    @_season("film new", "season new")
    def test_one_removed_from_the_key_it_was_given_to_is_given_to_none_in_another_library(
        self, conn, freezer, library, monkeypatch, run, still_listed
    ):
        library.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        library.scan("new")
        assert _actions(conn, "new") == ["protected_forever"]
        _remove_keep(conn, "new", monkeypatch)

        for _ in range(2):
            freezer.tick(60)
            getattr(library, run)(*(["new"] if still_listed else []), "newer")

        assert "protected_forever" not in _actions(conn, "newer")

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_season("season new")
    def test_a_whole_show_keep_removed_from_the_key_it_was_given_to_is_given_to_none_in_another_library(
        self, conn, freezer, library, run
    ):
        library.sync("old")
        _keep_show(conn)
        freezer.tick(60)
        library.scan("new")
        assert _is_kept(conn, "show", "new")
        with conn:
            conn.execute("DELETE FROM kept_shows WHERE show_rating_key = 'show-new'")
            log_audit(conn, "show-new", "removed_show_keep", "removed", actor="admin")

        for _ in range(2):
            freezer.tick(60)
            getattr(library, run)("newer")

        assert not _is_kept(conn, "show", "newer")

    @_season("season")
    def test_nobody_can_remove_it_while_it_is_given(
        self, conn, db_path, freezer, library, monkeypatch
    ):
        ids, library.ids["new"] = library.ids["new"], {}
        library.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync("new")
        library.ids["new"] = ids
        other = sqlite3.connect(db_path, timeout=0)
        locked = []

        def asked(*_args):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("DELETE FROM scheduled_actions")
            locked.append(True)
            return False

        monkeypatch.setattr(rekeyed, "_keep_removed_since", asked)

        library.sync("new")

        assert locked


class TestAnotherTitle:
    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_KEPT
    @pytest.mark.parametrize("matches", [[18, 9], [8, 18], [8, 18, None], [0], [""]], ids=str)
    @_season()
    def test_is_scheduled_though_its_tvdb_id_was_that_of_an_earlier_match(
        self, conn, freezer, library, kept, matches, lists
    ):
        library.ids["new"] = {"tvdb_id": 8 if matches[-1] not in (0, "") else matches[-1]}
        for tvdb_id in matches:
            library.ids["old"] = {"tvdb_id": tvdb_id}
            library.sync("old")
        library.ids["old"] = {}
        library.sync("old")
        _keep_either(conn, library, kept)
        for keys in lists:
            freezer.tick(60)
            library.scan(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_KEPT
    @pytest.mark.parametrize("after", [{"tmdb_id": 17}, None])
    @_season()
    def test_is_scheduled_though_its_tvdb_id_was_had_before_another_tmdb_id(
        self, conn, freezer, library, kept, after, lists
    ):
        library.ids["new"] = {"tvdb_id": 8}
        for ids in ({"tvdb_id": 8}, {"tmdb_id": 17}, after or {}):
            library.ids["old"] = ids
            library.sync("old")
        _keep_either(conn, library, kept)
        for keys in lists:
            freezer.tick(60)
            library.scan(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

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
            *((f"{kind} {key}", {"old"}) for kind in ("season", "film") for key in ("new", "old")),
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


def _without(how, library, kept_in):
    """Make a scan that does not read the library *kept_in*, as *how* says."""
    if how == "down":
        library.down = {kept_in}
        return library.scan
    left = [lib for lib in ("1", "2", "3", "4") if lib != kept_in]
    return functools.partial(library.scan, library_ids=left)


@pytest.mark.parametrize("how", ["left_out", "down"])
@pytest.mark.parametrize(
    ("library", "kept_in"), [("film new", "1"), ("season new", "2")], indirect=["library"]
)
class TestLibraryNotRead:
    """That Plex lists a title no more is known only of a library that was read."""

    def test_a_copy_seen_beside_the_kept_one_is_deleted_when_due(
        self, conn, freezer, library, kept_in, how
    ):
        library.sync("old", "new")
        _keep(conn, "old")
        if library.kind == "season":
            _keep_show(conn)
        _schedule(conn, "new")

        freezer.tick(60)
        _without(how, library, kept_in)("old", "new")

        assert not library.file("new").exists()

    def test_a_copy_seen_beside_the_watched_one_is_scheduled(
        self, conn, freezer, library, kept_in, how
    ):
        library.watched = {"old": _NOW - timedelta(days=1)}
        library.sync("old", "new")

        freezer.tick(60)
        _without(how, library, kept_in)("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    def test_one_never_seen_beside_the_kept_one_may_be_it_and_is_not_scheduled(
        self, conn, freezer, library, kept_in, how
    ):
        library.sync("old")
        _keep(conn, "old")

        freezer.tick(60)
        _without(how, library, kept_in)("new")

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize("listed", [True, False])
    def test_the_next_read_tells(self, conn, freezer, library, kept_in, how, listed):
        library.sync("old")
        _keep(conn, "old")
        freezer.tick(60)
        _without(how, library, kept_in)("new")
        library.down = set()

        freezer.tick(60)
        library.scan(*(["old"] if listed else []), "new")

        assert _actions(conn, "new") == ["scheduled_deletion" if listed else "protected_forever"]

    def test_one_last_read_as_gone_stays_gone(
        self, conn, freezer, library, kept_in, how, not_removed
    ):
        library.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync("new")
        _schedule(conn, "new")

        freezer.tick(60)
        _without(how, library, kept_in)("new")

        assert library.file("new").exists()


@pytest.mark.parametrize(
    ("library", "kept_in"), [("film new", "1"), ("season new", "2")], indirect=["library"]
)
def test_one_an_older_version_stored_is_not_deleted_while_the_kept_ones_library_is_not_read(
    conn, db_path, freezer, library, kept_in
):
    library.sync("old")
    _keep(conn, "old")
    library.down = {kept_in}
    freezer.tick(60)
    library.scan("new")
    with conn:
        conn.execute("UPDATE media_items SET first_listed_at = NULL")
    _schedule(conn, "new")
    init_db(str(db_path)).close()

    freezer.tick(60)
    library.scan("new")

    assert library.file("new").exists()


@pytest.fixture
def not_removed(conn, monkeypatch):
    """What a list lacks stays, as an item does whose deletion is not booked yet."""
    monkeypatch.setattr(
        delete.repository,
        "fetch_unbooked_media_ids",
        lambda _: {row["id"] for row in conn.execute("SELECT id FROM media_items")},
    )


class TestUnlistedButNotRemoved:
    @_season("film new", "season new")
    def test_one_listed_again_is_another_title(self, conn, freezer, library, not_removed):
        library.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)
        library.sync("new")

        freezer.tick(60)
        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @_season("season new")
    def test_any_season_of_a_kept_shows_other_key_is_not_scheduled(
        self, conn, freezer, library, not_removed
    ):
        library.ids["new"]["season_number"] = 2
        library.sync("old", "new")
        _keep_show(conn)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == []

    @_season("season new")
    def test_a_season_new_to_a_copy_of_a_kept_show_in_a_library_not_read_is_scheduled(
        self, conn, freezer, library
    ):
        library.elsewhere.add("newer")
        library.ids["newer"] |= {"show_rating_key": "show-new", "season_number": 2}
        library.sync("old", "new")
        _keep_show(conn)

        freezer.tick(60)
        library.scan("new", "newer", library_ids=["4"])

        assert _actions(conn, "newer") == ["scheduled_deletion"]

    @_season("film new", "season new")
    def test_one_kept_while_the_list_was_made_is_not_known_to_be_gone(self, conn, freezer, library):
        library.sync("old", "new")
        since = datetime.now(UTC).isoformat()
        _keep(conn, "old")

        delete.remove_orphans(conn, set(), {1, 2}, listed_since=since)

        assert not rekeyed.is_kept_under_another_key(conn, "new")

    @_season("film new")
    def test_a_list_too_short_to_trust_tells_of_no_title_that_plex_lists_it_no_more(
        self, conn, freezer, library
    ):
        library.sync("old", "new")
        _keep(conn, "old")
        freezer.tick(60)

        delete.remove_orphans(conn, set(), {1}, listed_since=datetime.now(UTC).isoformat())

        assert not rekeyed.is_kept_under_another_key(conn, "new")

    @_season("film new", "season new")
    def test_a_deletion_of_an_earlier_scan_stays_scheduled(self, conn, freezer, library):
        library.scan("old", "new")
        for _ in range(2):
            freezer.tick(60)
            library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]
        scheduled = conn.execute("SELECT scheduled_at FROM scheduled_actions").fetchall()
        assert [r["scheduled_at"] for r in scheduled] == [_NOW.isoformat()]


@_season("film new", "film old", "season new", "season old")
@pytest.mark.parametrize("why", ["kept", "watched"])
def test_a_copy_new_beside_it_in_another_library_is_scheduled_by_the_scan_that_first_sees_both(
    conn, freezer, library, monkeypatch, why
):
    library.watched = {"old": _NOW - timedelta(days=1)} if why == "watched" else {}
    library.sync("old")
    if why == "kept":
        _keep(conn, "old")
    history = library._history

    def slow(key):
        freezer.tick(1)
        return history(key)

    monkeypatch.setattr(library, "_history", slow)

    library.scan("old", "new")

    assert _actions(conn, "new") == ["scheduled_deletion"]


class TestWatchedLately:
    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_SEQUENCES
    @_season(*_MOVES, *_FILM_MOVES)
    def test_a_title_is_not_scheduled_after_it_moved(self, conn, freezer, library, lists, run):
        library.watched = {"old": _NOW - timedelta(days=1)}
        library.sync("old")
        for keys in lists:
            freezer.tick(60)
            library.sync(*keys)

        freezer.tick(60)
        getattr(library, run)("new")
        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize(
        ("library", "ids"),
        [("season", {}), ("season", {"tmdb_id": None, "tvdb_id": 8}), ("film", {})],
        indirect=["library"],
    )
    def test_a_match_plex_lost_still_tells_the_title(self, conn, freezer, library, ids):
        library.ids["old"] |= ids
        library.watched = {"old": _NOW - timedelta(days=1)}
        library.sync("old")
        library.ids["old"] = {}
        for keys in (["old"], []):
            freezer.tick(60)
            library.sync(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ({}, {"tmdb_id": 17, "tvdb_id": 18}),
            ({}, {"season_number": 2}),
            ({}, {"tmdb_id": None, "tvdb_id": None}),
            *(
                ({"tmdb_id": empty, "tvdb_id": empty}, {"tmdb_id": empty, "tvdb_id": empty})
                for empty in (None, 0, "")
            ),
        ],
    )
    @_season("season", "season new")
    def test_another_season_is_scheduled(self, conn, freezer, library, old, new, lists):
        library.ids["old"] |= old
        library.ids["new"] |= new
        library.watched = {"old": _NOW - timedelta(days=1)}
        library.sync("old")
        for keys in lists:
            freezer.tick(60)
            library.sync(*keys)

        freezer.tick(60)
        library.scan("new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @_season("season new", "season old", "film new", "film old")
    def test_a_copy_listed_beside_it_in_another_library_is_scheduled(
        self, conn, freezer, library, run
    ):
        library.watched = {"old": _NOW - timedelta(days=1)}
        for _ in range(2):
            freezer.tick(60)
            getattr(library, run)("old", "new")

        freezer.tick(60)
        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @_season("season new", "film new")
    def test_a_copy_in_another_library_is_scheduled_once_the_watched_one_is_back(
        self, conn, freezer, library
    ):
        library.watched = {"old": _NOW - timedelta(days=1)}
        for keys in (["old"], [], ["old"]):
            freezer.tick(60)
            library.sync(*keys)

        library.scan("old", "new")

        assert _actions(conn, "new") == ["scheduled_deletion"]

    @_season("season")
    def test_looking_for_it_does_not_read_the_library(self, conn, library):
        library.sync("old", "new")
        queries = []
        conn.set_trace_callback(queries.append)
        carry_last_watched(conn, "new")
        conn.set_trace_callback(None)

        plan = [
            row["detail"]
            for query in queries
            if query.startswith("SELECT")
            for row in conn.execute(f"EXPLAIN QUERY PLAN {query}")
        ]
        assert plan
        assert not [s for s in plan if s.startswith("SCAN") and "subquery" not in s.lower()]


class TestProgress:
    @pytest.fixture
    def played(self, conn, library):
        with conn:
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at) "
                "VALUES ('amy', 'h', '2020')"
            )
        if library.kind == "season":
            for key, ids in library.ids.items():
                ids["episodes"] = [
                    {"key": f"{key}-e{n}", "number": n, "path": f"/{key}/E{n}"} for n in (1, 2)
                ]
        library.sync("old")
        save_progress(
            conn,
            username="amy",
            rating_key="old" if library.kind == "film" else "old-e2",
            media_item_id="old",
            show_rating_key=None if library.kind == "film" else "show-old",
            position_s=600.0,
            duration_s=6000.0,
            played_s=600.0,
            now=_NOW,
        )
        return library

    @staticmethod
    def _progress(conn):
        return [
            (r["rating_key"], r["media_item_id"], r["show_rating_key"], r["position_s"])
            for r in conn.execute("SELECT * FROM watch_progress")
        ]

    @pytest.mark.parametrize("run", ["sync", "scan"])
    @pytest.mark.parametrize("lists", [[], [["old", "new"]], [[]]], ids=str)
    @_season(*_MOVES, *_FILM_MOVES)
    def test_a_place_follows_the_title(self, conn, freezer, played, lists, run):
        for keys in lists:
            freezer.tick(60)
            played.sync(*keys)
        freezer.tick(60)
        getattr(played, run)("new")
        freezer.tick(60)
        getattr(played, run)("new")

        key, show = ("new", None) if played.kind == "film" else ("new-e2", "show-new")
        assert self._progress(conn) == [(key, "new", show, 600.0)]

    @pytest.mark.parametrize("ids", [{"tmdb_id": 17, "tvdb_id": 18}, {"season_number": 2}])
    @_season("season")
    def test_a_place_does_not_go_to_another_title(self, conn, freezer, played, ids):
        played.ids["new"] |= ids
        freezer.tick(60)
        played.sync("new")

        assert self._progress(conn) == []

    @_season("season")
    def test_a_place_waits_when_two_episodes_have_its_number(self, conn, freezer, played):
        for episode in played.ids["new"]["episodes"]:
            episode["number"] = 2
        freezer.tick(60)
        played.sync("new")

        assert self._progress(conn) == []
