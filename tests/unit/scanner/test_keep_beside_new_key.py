"""Tests that a kept film or season keeps its files when Plex lists a new key beside the kept one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

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
        self.places = {}

    def file(self, key):
        return self.folder(key) / ("film.mkv" if self.kind == "film" else "E1.mkv")

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

    def _run(self, run, keys):
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
        plex.get_movie_items.return_value = others + (items if self.kind == "film" else [])
        plex.get_show_seasons.return_value = items if self.kind == "season" else []
        plex.get_watch_history.return_value = []
        plex.get_season_watch_history.return_value = []
        engine = ScanEngine(
            conn=self._conn,
            plex_client=plex,
            library_ids=["1", "2"],
            library_types={"1": "movie", "2": "show"},
            secret_key="0123456789abcdef" * 4,
        )
        getattr(engine, run)()

    def sync(self, *keys):
        self._run("sync_library", keys)

    def scan(self, *keys):
        self._run("run_scan", keys)


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
        "VALUES ('plex_libraries', '[\"1\", \"2\"]', '2026')"
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
        token_used=False,
        **{"delete_status": "pending"} | kwargs,
    )
    conn.commit()


def _actions(conn, media_id):
    return [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id = ? ORDER BY id", (media_id,)
        )
    ]


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


class TestKeyBesideAKeptOne:
    @pytest.mark.parametrize("both", [("old", "new"), ("new", "old")])
    def test_is_not_scheduled(self, conn, library, both):
        library.sync("old")
        _keep(conn, "old")

        library.scan(*both)

        assert _actions(conn, "new") == []

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ({"season_number": 1}, {"season_number": 2}),
            ({"tmdb_id": 1}, {"tmdb_id": 2}),
            ({"tvdb_id": 1}, {"tvdb_id": 2}),
            ({"tmdb_id": 1, "tvdb_id": 3}, {"tmdb_id": 2, "tvdb_id": 3}),
        ],
    )
    def test_is_scheduled_and_given_no_keep_when_it_is_another_title_there(
        self, conn, freezer, library, old, new
    ):
        library.ids = {"old": old, "new": new}
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
