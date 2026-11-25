"""A due deletion leaves a file that a keep or a late watch of a title Plex lists no more holds."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner.engine import ScanEngine
from mediaman.web.repository.kept import delete_protections
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action

_SEASON = {"media_type": "tv_season", "show_title": "Title"}


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class _GoneLibrary:
    """Plex lists library 1 no more, and lists what it likes in 2 (films) and 4 (shows)."""

    @pytest.fixture(autouse=True)
    def _library(self, conn, tmp_path, monkeypatch, freezer):
        freezer.move_to(datetime(2026, 11, 24, 12, tzinfo=UTC))
        monkeypatch.setattr(
            "mediaman.scanner.engine._refresh_recommendations", lambda *a, **k: None
        )
        monkeypatch.setattr("mediaman.scanner.engine._send_newsletter", lambda **k: None)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        self.conn, self.freezer = conn, freezer
        self.folder = tmp_path / "Title"
        self.folder.mkdir()
        self.ticked = ("2", "4")
        self.listed = {"2": [], "4": []}
        self.plex = MagicMock()
        self.plex.get_movie_items.side_effect = lambda library: self.listed.get(library, [])
        self.plex.get_show_seasons.side_effect = self.listed.__getitem__
        self.plex.get_watch_history.return_value = []
        self.plex.get_season_watch_history.return_value = []

    def _file(self, name):
        file = self.folder / name
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b"data")
        return file

    def _gone(self, path, files, library=1, **fields):
        """Store the title "10" as *library* had it, with the TMDB id 7."""
        insert_media_item(
            self.conn,
            id="10",
            plex_rating_key="10",
            plex_library_id=library,
            file_path=str(path),
            **fields,
        )
        self.conn.execute(
            "UPDATE media_items SET tmdb_id = 7, tracked_files = ? WHERE id = '10'",
            (json.dumps([{"path": str(file), "size": 4} for file in files]),),
        )
        self.conn.commit()

    def _keep(self, **fields):
        insert_scheduled_action(
            self.conn,
            media_item_id="10",
            token="keep-10",
            token_used=True,
            **({"action": "protected_forever", "execute_at": None} | fields),
        )
        self.conn.commit()

    def _keep_show(self):
        insert_kept_show(
            self.conn, show_rating_key="s1", show_title="Title", action="protected_forever"
        )
        self.conn.commit()

    def _list(self, key, library, path, files, **fields):
        """Have Plex list *key* from now on: added long ago and never watched."""
        self.listed[str(library)].append(
            {
                "plex_rating_key": key,
                "title": key,
                "added_at": datetime(2020, 1, 1, tzinfo=UTC),
                "file_path": str(path),
                "file_size_bytes": 4,
                "tracked_files": [{"path": str(file), "size": 4} for file in files],
                **fields,
            }
        )

    def _scan(self, times):
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            "VALUES ('plex_libraries', ?, '2026')",
            (json.dumps(self.ticked),),
        )
        self.conn.commit()
        for _ in range(times):
            ScanEngine(
                conn=self.conn,
                plex_client=self.plex,
                library_ids=list(self.ticked),
                library_types={"2": "movie", "4": "show"},
                secret_key="test-key",
            ).run_scan()
            self.freezer.tick(timedelta(hours=2))

    def _scan_until_pruned(self):
        self._scan(2)
        assert not self.conn.execute("SELECT 1 FROM media_items WHERE id = '10'").fetchone()

    def _scan_until_due(self, *keys):
        """Scan twice more, which schedules *keys*, then once their grace period is over."""
        self._scan(2)
        assert self._scheduled() == set(keys)
        self._scan_past_grace()

    def _scan_past_grace(self):
        self.freezer.tick(timedelta(days=15))
        self._scan(1)

    def _scheduled(self):
        rows = self.conn.execute(
            "SELECT media_item_id FROM scheduled_actions WHERE action = 'scheduled_deletion'"
        )
        return {row[0] for row in rows}

    def _said(self, key):
        rows = self.conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id = ? "
            "AND action = 'delete_awaiting_scan'",
            (key,),
        )
        return [row[0] for row in rows]


class TestFileOfATitleOfAGoneLibraryListedLaterAsAnother(_GoneLibrary):
    """Once the title of library 1 is pruned, Plex lists a file of it as another title."""

    @pytest.mark.parametrize("ticked", [("2", "4"), ("1", "2", "4")], ids=["unticked", "ticked"])
    def test_a_file_of_a_kept_film_stays_though_a_film_with_other_files_has_it(self, ticked):
        self.ticked = ticked
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(own, [own, shared])
        self._keep()
        self._scan_until_pruned()
        self._list("20", 2, other, [other, shared])

        self._scan_until_due("20")

        assert shared.exists()

    @pytest.mark.parametrize("kept", ["season", "show"])
    def test_the_files_of_a_kept_season_stay_though_listed_as_another_season(self, kept):
        episode = self._file("E1.mkv")
        self._gone(self.folder, [episode], season_number=1, show_rating_key="s1", **_SEASON)
        self._keep() if kept == "season" else self._keep_show()
        self._scan_until_pruned()
        self._list(
            "20", 4, self.folder, [episode], season_number=2, show_rating_key="s2", **_SEASON
        )

        self._scan_until_due("20")

        assert episode.exists()

    def test_the_file_of_a_kept_film_stays_though_listed_as_a_season(self):
        film = self._file("film.mkv")
        self._gone(film, [film])
        self._keep()
        self._scan_until_pruned()
        self._list("20", 4, self.folder, [film], season_number=1, show_rating_key="s2", **_SEASON)

        self._scan_until_due("20")

        assert film.exists()

    def test_a_file_of_a_film_watched_lately_stays_until_the_watch_is_too_long_ago(self):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(own, [own, shared], last_watched_at=datetime.now(UTC) - timedelta(days=1))
        self._scan_until_pruned()
        self._list("20", 2, other, [other, shared])

        self._scan_until_due("20")
        assert shared.exists()
        assert "watched lately" in self._said("20")[0]

        self.freezer.tick(timedelta(days=20))
        self._scan(1)
        assert not shared.exists()

    def test_the_deletion_held_stays_as_it_was_scheduled_and_says_once_that_a_keep_holds_it(self):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(own, [own, shared])
        self._keep()
        self._scan_until_pruned()
        self._list("20", 2, other, [other, shared])
        self._scan(1)
        scheduled = self.conn.execute(
            "SELECT id, action, execute_at FROM scheduled_actions WHERE media_item_id = '20'"
        ).fetchall()

        self._scan_until_due("20")
        self._scan(1)

        after = self.conn.execute(
            "SELECT id, action, execute_at FROM scheduled_actions WHERE media_item_id = '20'"
        ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in scheduled]
        assert other.exists()
        said = self._said("20")
        assert len(said) == 1
        assert "kept" in said[0]

    def test_a_folder_that_holds_a_title_with_such_a_file_waits_too(self):
        film, part, episode = self._file("film.mkv"), self._file("part.mkv"), self._file("E1.mkv")
        self._gone(film, [film])
        self._keep()
        self._scan_until_pruned()
        self._list(
            "20", 4, self.folder, [episode], season_number=1, show_rating_key="s2", **_SEASON
        )
        self._list("21", 2, part, [part, film])

        self._scan_until_due("20", "21")

        assert (film.exists(), episode.exists()) == (True, True)

    def test_a_file_spelled_another_way_is_held_too(self):
        a, b, d = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(a, [a, b])
        self._keep()
        self._scan_until_pruned()
        self._list("20", 2, d, [d, f"{self.folder}/./b.mkv"])

        self._scan_until_due("20")

        assert b.exists()

    def test_a_title_with_none_of_its_files_is_deleted_as_before(self):
        own, other = self._file("a.mkv"), self._file("d.mkv")
        self._gone(own, [own])
        self._keep()
        self._scan_until_pruned()
        self._list("20", 2, other, [other])

        self._scan_until_due("20")

        assert (own.exists(), other.exists()) == (True, False)

    def test_once_the_title_is_back_and_has_its_keep_another_with_its_file_is_not_held(self):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(own, [own, shared])
        self._keep()
        self._scan_until_pruned()
        self._list("30", 2, own, [own, shared])
        self._list("20", 2, other, [other, shared])

        self._scan_until_due("20")

        # The file the two have stays as one of a title in the library; nothing holds the other.
        assert (own.exists(), shared.exists(), other.exists()) == (True, True, False)

    def test_a_keep_that_ran_out_holds_no_file(self):
        own, shared, other = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(own, [own, shared])
        self._keep(action="snoozed", execute_at=(datetime.now(UTC) + timedelta(days=7)).isoformat())
        self._scan_until_pruned()
        self._list("20", 2, other, [other, shared])

        self._scan_until_due("20")

        assert not shared.exists()

    def test_a_show_keep_a_person_removed_holds_no_file(self):
        episode = self._file("E1.mkv")
        self._gone(self.folder, [episode], season_number=1, show_rating_key="s1", **_SEASON)
        self._keep_show()
        self._scan_until_pruned()
        self._list(
            "20", 4, self.folder, [episode], season_number=2, show_rating_key="s2", **_SEASON
        )
        self._scan(2)
        self.conn.execute("DELETE FROM kept_shows WHERE show_rating_key = 's1'")
        self.conn.commit()

        self._scan_until_due("20")

        assert not episode.exists()


class TestFilesOfAKeepATitleTook(_GoneLibrary):
    """Film "10" is kept with the files a and b; Plex lists a as the film "30", which takes the keep."""

    def _listed_as_two(self):
        """Prune the kept film, then have Plex list its files as the films "30" and "20"."""
        a, b = self._file("a.mkv"), self._file("b.mkv")
        self._gone(a, [a, b])
        self._keep()
        self._scan_until_pruned()
        self._list("30", 2, a, [a])
        self._list("20", 2, b, [b])
        return b

    @pytest.mark.parametrize("taken", ["before the prune", "after the prune"])
    @pytest.mark.parametrize("other", ["alone", "with another file"])
    def test_the_file_it_lacks_stays_though_listed_as_another_film(self, taken, other):
        a, b, d = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(a, [a, b])
        self._keep()
        if taken == "before the prune":
            self._list("30", 2, a, [a])
        self._scan_until_pruned()
        if taken == "after the prune":
            self._list("30", 2, a, [a])
        if other == "alone":
            self._list("20", 2, b, [b])
        else:
            self._list("20", 2, d, [d, b])

        self._scan_until_due("20")

        assert b.exists()

    def test_it_stays_in_a_folder_of_its_own_and_whatever_ids_plex_gives_the_two(self):
        a, b = self._file("a.mkv"), self._file("Other/b.mkv")
        self._gone(a, [a, b])
        self._keep()
        self._scan_until_pruned()
        self._list("30", 2, a, [a], tmdb_id=8)
        self._list("20", 2, b, [b], tmdb_id=9)

        self._scan_until_due("20")

        assert b.exists()

    @pytest.mark.parametrize("taken", ["before the prune", "after the prune"])
    @pytest.mark.parametrize("kept", ["season", "show"])
    def test_the_episode_it_lacks_stays_though_listed_as_another_season(self, kept, taken):
        one, two = self._file("S1/E1.mkv"), self._file("S2/E2.mkv")
        self._gone(one.parent, [one, two], season_number=1, show_rating_key="s1", **_SEASON)
        self._keep() if kept == "season" else self._keep_show()
        took = {"season_number": 1, "show_rating_key": "s2", **_SEASON}
        if taken == "before the prune":
            self._list("30", 4, one.parent, [one], **took)
        self._scan_until_pruned()
        if taken == "after the prune":
            self._list("30", 4, one.parent, [one], **took)
        self._list("20", 4, two.parent, [two], season_number=2, show_rating_key="s3", **_SEASON)

        self._scan_until_due("20")

        assert two.exists()

    def test_the_file_a_film_that_took_it_by_its_other_file_lacks_stays(self):
        a, b, d = self._file("a.mkv"), self._file("b.mkv"), self._file("d.mkv")
        self._gone(a, [a, b])
        self._keep()
        self._scan_until_pruned()
        self._list("30", 2, b, [b])
        self._list("20", 2, d, [d, a])

        self._scan_until_due("20")

        assert a.exists()

    def test_the_episode_a_show_that_took_the_keep_by_its_ids_lacks_stays(self):
        one, two, new = self._file("S1/E1.mkv"), self._file("S2/E2.mkv"), self._file("S9/E9.mkv")
        self._gone(
            one.parent, [one, two], library=4, season_number=1, show_rating_key="s1", **_SEASON
        )
        self._keep_show()
        self._scan(1)
        assert not self.conn.execute("SELECT 1 FROM media_items WHERE id = '10'").fetchone()
        took = {"season_number": 1, "show_rating_key": "s2", "tmdb_id": 7, **_SEASON}
        self._list("30", 4, new.parent, [new], **took)
        self._list("20", 4, two.parent, [two], season_number=2, show_rating_key="s3", **_SEASON)

        self._scan_until_due("20")

        assert two.exists()

    @pytest.mark.parametrize("ended", ["removed by a person", "ran out"])
    def test_a_keep_that_ended_before_the_other_film_was_due_holds_nothing(self, ended):
        a, b = self._file("a.mkv"), self._file("b.mkv")
        self._gone(a, [a, b])
        if ended == "ran out":
            runs_out = datetime.now(UTC) + timedelta(days=7)
            self._keep(action="snoozed", execute_at=runs_out.isoformat())
        else:
            self._keep()
        self._scan_until_pruned()
        self._list("30", 2, a, [a])
        self._list("20", 2, b, [b])
        self._scan(2)
        if ended == "removed by a person":
            with self.conn:
                delete_protections(self.conn, "30")

        self._scan_past_grace()

        assert not b.exists()

    @pytest.mark.parametrize("removed", [False, True])
    def test_what_is_held_follows_the_new_key_plex_gives_the_title_that_took_the_keep(
        self, removed
    ):
        b = self._listed_as_two()
        self._scan(2)
        self.listed["2"][0]["plex_rating_key"] = "31"
        self._scan(1)
        assert not self.conn.execute("SELECT 1 FROM media_items WHERE id = '30'").fetchone()
        if removed:
            with self.conn:
                delete_protections(self.conn, "31")

        self._scan_past_grace()

        assert b.exists() is not removed
