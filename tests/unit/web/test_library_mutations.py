"""Tests for POST /api/media/{id}/delete, /keep, and /api/media/redownload."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from mediaman.db import get_db
from mediaman.services.infra import SafeHTTPError
from mediaman.web.repository.delete_intents import reconcile_pending_delete_intents
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import _DELETE_LIMITER, _KEEP_LIMITER
from mediaman.web.routes.library_api import delete as delete_route
from mediaman.web.routes.library_api import router as library_api_router
from mediaman.web.routes.library_api.redownload import _REDOWNLOAD_LIMITER
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action

MOVIE_DIR = "/media/movies/Test Movie (2020)"
MOVIE_FILE = {"path": f"{MOVIE_DIR}/Test Movie (2020).mkv", "size": 1_000_000}
SEASON_DIR = "/media/tv/The Show/Season 01"
SEASON_FILES = [
    {"path": f"{SEASON_DIR}/The Show - S01E01.mkv", "size": 500},
    {"path": f"{SEASON_DIR}/The Show - S01E02.mkv", "size": 600},
]
RADARR = "mediaman.web.routes.library_api.build_radarr_from_db"
SONARR = "mediaman.web.routes.library_api.build_sonarr_from_db"


def _set_ids(conn, media_id: str, **fields) -> None:
    for column, value in fields.items():
        if column == "tracked_files" and value is not None:
            value = json.dumps(value)
        conn.execute(f"UPDATE media_items SET {column} = ? WHERE id = ?", (value, media_id))
    conn.commit()


def _insert_movie(
    conn,
    media_id: str = "m1",
    radarr_id: int | None = 101,
    tmdb_id: int | None = 550,
    tracked_files: list | None = None,
) -> None:
    """Insert a movie row whose ids and files match `_radarr()`'s entry."""
    tracked = [MOVIE_FILE] if tracked_files is None else tracked_files
    insert_media_item(
        conn,
        id=media_id,
        title="Test Movie",
        media_type="movie",
        plex_rating_key="rk1",
        file_path=tracked[0]["path"] if tracked else "/media/movie.mkv",
        file_size_bytes=1_000_000,
        radarr_id=radarr_id,
    )
    _set_ids(conn, media_id, tmdb_id=tmdb_id, tracked_files=tracked)


def _insert_tv_season(
    conn,
    media_id: str = "s1",
    sonarr_id: int | None = 202,
    season: int = 1,
    tvdb_id: int | None = 81189,
    show_rating_key: str = "show-rk",
    tracked_files: list | None = None,
) -> None:
    """Insert a season row whose ids and files match `_sonarr()`'s series."""
    insert_media_item(
        conn,
        id=media_id,
        title="The Show",
        media_type="tv_season",
        plex_rating_key=f"rk-{media_id}",
        file_path=SEASON_DIR,
        file_size_bytes=2_000_000,
        sonarr_id=sonarr_id,
        season_number=season,
        show_title="The Show",
        show_rating_key=show_rating_key,
    )
    tracked = SEASON_FILES if tracked_files is None else tracked_files
    _set_ids(conn, media_id, tvdb_id=tvdb_id, tracked_files=tracked)


def _radarr(**entry_overrides) -> MagicMock:
    """A Radarr client whose live entry is the movie `_insert_movie` stores."""
    entry = {
        "id": 101,
        "tmdbId": 550,
        "path": "/movies/Test Movie (2020)",
        "hasFile": True,
        "movieFile": {"relativePath": "Test Movie (2020).mkv", "size": 1_000_000},
        **entry_overrides,
    }
    client = MagicMock()
    client.get_movie_by_id.return_value = entry
    client.get_movie_by_tmdb.return_value = entry
    return client


def _sonarr(extra_files: list | None = None) -> MagicMock:
    """A Sonarr client whose series and season 1 files are the ones `_insert_tv_season` stores."""
    series = {
        "id": 202,
        "tvdbId": 81189,
        "path": "/tv/The Show",
        "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
    }
    files = [
        {
            "id": 1,
            "seasonNumber": 1,
            "relativePath": "Season 01/The Show - S01E01.mkv",
            "size": 500,
        },
        {
            "id": 2,
            "seasonNumber": 1,
            "relativePath": "Season 01/The Show - S01E02.mkv",
            "size": 600,
        },
        *(extra_files or []),
    ]
    listed = {"files": files}

    def delete_season_files(ids: list[int]) -> None:
        listed["files"] = [f for f in listed["files"] if f["id"] not in ids]

    client = MagicMock()
    client.get_series_by_id.return_value = series
    client.get_series_by_tvdb.return_value = series
    client.get_episode_files.side_effect = lambda _series_id: list(listed["files"])
    client.delete_episode_files.side_effect = delete_season_files
    return client


def _app(app_factory, conn):
    return app_factory(library_router, library_api_router, conn=conn)


def _audit_actions(conn, media_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT action FROM audit_log WHERE media_item_id = ?", (media_id,)
    ).fetchall()
    return [r["action"] for r in rows]


def _row_exists(conn, media_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM media_items WHERE id = ?", (media_id,)).fetchone() is not None
    )


class TestMediaDelete:
    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    def test_delete_requires_auth(self, app_factory, conn):
        """DELETE endpoint returns 401 when no session cookie is present."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/media/m1/delete")
        assert resp.status_code == 401

    def test_delete_nonexistent_returns_403(self, app_factory, authed_client, conn):
        """Deleting an unknown media_id returns 403, not 404.

        Returning 404 leaks whether a given media_id exists. With auth
        already confirmed, an unknown id is treated as forbidden access
        instead of a discoverable resource boundary.
        """
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/does-not-exist/delete")
        assert resp.status_code == 403

    def test_movie_is_deleted_through_radarr_and_its_identity_kept(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        assert resp.json()["id"] == "m1"
        assert "Radarr" in resp.json()["message"]
        assert "exclusion" in resp.json()["message"]
        radarr.delete_movie.assert_called_once_with(101)
        assert not _row_exists(conn, "m1")
        audit = conn.execute(
            "SELECT id FROM audit_log WHERE media_item_id = 'm1' AND action = 'deleted'"
        ).fetchone()
        deleted = conn.execute(
            "SELECT media_type, tmdb_id, title FROM deleted_media WHERE audit_id = ?",
            (audit["id"],),
        ).fetchone()
        assert tuple(deleted) == ("movie", 550, "Test Movie")

    def test_movie_without_a_stored_radarr_id_is_found_by_tmdb_id(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn, radarr_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        radarr.get_movie_by_tmdb.assert_called_once_with(550)
        radarr.delete_movie.assert_called_once_with(101)
        assert not _row_exists(conn, "m1")

    def test_season_is_deleted_and_unmonitored_through_sonarr(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn, sonarr_id=202, season=1)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 200
        assert resp.json()["message"].startswith("Season 1 of The Show deleted through Sonarr")
        sonarr.delete_episode_files.assert_called_once_with([1, 2])
        sonarr.unmonitor_season.assert_called_once_with(202, 1)
        sonarr.delete_series.assert_not_called()
        assert not _row_exists(conn, "s1")
        deleted = conn.execute(
            "SELECT media_type, tvdb_id, season_number FROM deleted_media"
        ).fetchone()
        assert tuple(deleted) == ("tv_season", 81189, 1)

    def test_only_the_verified_episode_files_are_deleted(self, app_factory, authed_client, conn):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()
        verified = sonarr.get_episode_files.side_effect
        imported = {"id": 9, "seasonNumber": 1, "relativePath": "Season 01/new.mkv", "size": 7}
        calls = {"n": 0}

        def import_lands_after_the_check(series_id):
            calls["n"] += 1
            return verified(series_id) + ([imported] if calls["n"] > 1 else [])

        sonarr.get_episode_files.side_effect = import_lands_after_the_check

        with patch(SONARR, return_value=sonarr):
            client.post("/api/media/s1/delete")

        sonarr.delete_episode_files.assert_called_once_with([1, 2])

    def test_season_without_a_stored_sonarr_id_is_found_by_tvdb_id(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn, sonarr_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 200
        sonarr.get_series_by_tvdb.assert_called_once_with(81189)
        sonarr.delete_episode_files.assert_called_once_with([1, 2])

    def test_last_season_with_no_files_left_keeps_the_show_in_sonarr(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 200
        sonarr.unmonitor_season.assert_called_once_with(202, 1)
        sonarr.delete_series.assert_not_called()
        assert "removed" not in resp.json()["message"]

    def test_delete_also_removes_scheduled_actions(self, app_factory, authed_client, conn):
        """Deleting a media item also prunes its scheduled_actions rows, a lapsed snooze too."""
        _insert_movie(conn)
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="snoozed",
            execute_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            token="tok1",
            token_used=True,
        )
        client = authed_client(_app(app_factory, conn), conn)

        with patch(RADARR, return_value=_radarr()):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        sa_row = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchone()
        assert sa_row is None

    def test_movie_is_verified_then_keep_rechecked_then_deleted(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        order: list[str] = []
        radarr = _radarr()
        radarr.delete_movie.side_effect = lambda _id: order.append("delete")
        real_verify = delete_route.verify_movie
        real_recheck = delete_route.recheck_deletable

        def verify(*args, **kwargs):
            order.append("verify")
            return real_verify(*args, **kwargs)

        def recheck(*args, **kwargs):
            order.append("recheck")
            return real_recheck(*args, **kwargs)

        with (
            patch(RADARR, return_value=radarr),
            patch.object(delete_route, "verify_movie", side_effect=verify),
            patch.object(delete_route, "recheck_deletable", side_effect=recheck),
        ):
            assert client.post("/api/media/m1/delete").status_code == 200

        assert order == ["verify", "recheck", "delete"]


class TestMediaDeleteRefusals:
    """A delete that cannot be confirmed safe answers 409 and changes nothing."""

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    def _assert_refused(self, resp, conn, media_id: str, client_mock, *, fact: str) -> None:
        assert resp.status_code == 409
        assert resp.json()["ok"] is False
        assert fact in resp.json()["message"]
        assert _row_exists(conn, media_id)
        assert _audit_actions(conn, media_id) == []
        if client_mock is not None:
            for name in ("delete_movie", "delete_episode_files", "delete_series"):
                getattr(client_mock, name).assert_not_called()

    def test_radarr_not_configured(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)

        with patch(RADARR, return_value=None):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", None, fact="Radarr is not set up")

    def test_sonarr_not_configured(self, app_factory, authed_client, conn):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)

        with patch(SONARR, return_value=None):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", None, fact="Sonarr is not set up")

    def test_movie_without_a_tmdb_id(self, app_factory, authed_client, conn):
        _insert_movie(conn, tmdb_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="TMDB")

    def test_season_without_a_tvdb_id(self, app_factory, authed_client, conn):
        _insert_tv_season(conn, tvdb_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", sonarr, fact="TVDB")

    def test_movie_radarr_does_not_have(self, app_factory, authed_client, conn):
        _insert_movie(conn, radarr_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()
        radarr.get_movie_by_tmdb.return_value = None

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="no entry")

    def test_show_sonarr_does_not_have(self, app_factory, authed_client, conn):
        _insert_tv_season(conn, sonarr_id=None)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()
        sonarr.get_series_by_tvdb.return_value = None

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", sonarr, fact="no entry")

    def test_a_404_on_the_lookup_is_not_taken_as_already_gone(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()
        radarr.get_movie_by_id.side_effect = SafeHTTPError(404, "Not Found", "radarr/")

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="no entry")

    def test_a_second_radarr_copy_with_a_different_file_is_refused(
        self, app_factory, authed_client, conn
    ):
        """Same TMDB id and folder name, but a 4K file Plex never reported."""
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr(
            movieFile={"relativePath": "Test Movie (2020) 2160p.mkv", "size": 40_000_000_000}
        )

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="different file")

    def test_a_season_with_a_sonarr_file_plex_does_not_show_is_refused(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr(
            extra_files=[
                {
                    "id": 9,
                    "seasonNumber": 1,
                    "relativePath": "Season 01/The Show - S01E03.mkv",
                    "size": 700,
                }
            ]
        )

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", sonarr, fact="Plex does not show")

    def test_scheduled_deletion_in_progress(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        insert_scheduled_action(conn, media_item_id="m1", action="scheduled_deletion")
        conn.execute("UPDATE scheduled_actions SET delete_status = 'deleting'")
        conn.commit()
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="in progress")

    def test_scheduled_deletion_starting_during_the_check(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        insert_scheduled_action(conn, media_item_id="m1", action="scheduled_deletion")
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()
        entry = radarr.get_movie_by_id.return_value

        def deletion_starts(_id):
            db = get_db()
            db.execute("UPDATE scheduled_actions SET delete_status = 'deleting'")
            db.commit()
            return entry

        radarr.get_movie_by_id.side_effect = deletion_starts

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="in progress")

    def test_another_item_using_the_movie_folder(self, app_factory, authed_client, conn):
        """Radarr's deleteFiles removes the whole folder, so a second library's copy blocks it."""
        _insert_movie(conn)
        _insert_movie(
            conn,
            "m2",
            radarr_id=None,
            tracked_files=[{"path": f"{MOVIE_DIR}/Test Movie (2020) 2160p.mkv", "size": 9}],
        )
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        self._assert_refused(resp, conn, "m1", radarr, fact="Another library item")

    def test_another_item_tracking_a_file_of_the_season(self, app_factory, authed_client, conn):
        _insert_tv_season(conn)
        _insert_tv_season(conn, "copy", show_rating_key="copy-show", tracked_files=SEASON_FILES[:1])
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", sonarr, fact="Another library item")

    def test_an_exact_copy_of_the_movie_in_another_library_root(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        _insert_movie(
            conn,
            "kids",
            radarr_id=None,
            tracked_files=[
                {
                    "path": "/media/movies-kids/Test Movie (2020)/Test Movie (2020).mkv",
                    "size": 1_000_000,
                }
            ],
        )
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/kids/delete")

        self._assert_refused(resp, conn, "kids", radarr, fact="More than one library item")

    def test_an_exact_copy_of_the_season_in_another_library_root(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        _insert_tv_season(
            conn,
            "kids",
            show_rating_key="kids-show",
            tracked_files=[
                {"path": f["path"].replace("/media/tv/", "/media/tv-kids/"), "size": f["size"]}
                for f in SEASON_FILES
            ],
        )
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with patch(SONARR, return_value=sonarr):
            resp = client.post("/api/media/s1/delete")

        self._assert_refused(resp, conn, "s1", sonarr, fact="More than one library item")

    def test_radarr_unreachable_on_the_check_deletes_nothing(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()
        radarr.get_movie_by_id.side_effect = requests.ConnectionError("refused")

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        radarr.delete_movie.assert_not_called()
        assert _row_exists(conn, "m1")
        assert _audit_actions(conn, "m1") == []


class TestMediaDeleteRefusesKeptItems:
    """An item the scanner keeps is never deleted from the Library."""

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    @pytest.mark.parametrize(
        ("action", "execute_in", "token_used"),
        [
            ("protected_forever", None, False),
            ("protected_forever", None, True),
            ("snoozed", timedelta(hours=2), False),
            ("snoozed", timedelta(days=30), True),
        ],
        ids=["forever", "forever-from-email", "snooze-ending-today", "snooze-from-email"],
    )
    def test_kept_movie_is_refused_and_left_in_place(
        self, app_factory, authed_client, conn, action, execute_in, token_used
    ):
        _insert_movie(conn)
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action=action,
            execute_at=(datetime.now(UTC) + execute_in).isoformat() if execute_in else None,
            token="tok1",
            token_used=token_used,
        )
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = _radarr()
        with patch(RADARR, return_value=mock_radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 409
        assert "kept" in resp.json()["message"]
        mock_radarr.delete_movie.assert_not_called()
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()

    def test_season_of_a_kept_show_is_refused(self, app_factory, authed_client, conn):
        _insert_tv_season(conn)
        insert_kept_show(conn, show_rating_key="show-rk", action="protected_forever")
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = _sonarr()
        with patch(SONARR, return_value=mock_sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 409
        mock_sonarr.delete_episode_files.assert_not_called()
        assert conn.execute("SELECT id FROM media_items WHERE id='s1'").fetchone()

    def test_keep_landing_during_the_check_is_refused(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()
        entry = radarr.get_movie_by_id.return_value

        def keep_lands(_id):
            insert_scheduled_action(
                get_db(), media_item_id="m1", action="protected_forever", token="tok1"
            )
            return entry

        radarr.get_movie_by_id.side_effect = keep_lands

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 409
        assert resp.json()["error"] == "kept"
        radarr.delete_movie.assert_not_called()
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
        assert conn.execute("SELECT id FROM audit_log WHERE action='deleted'").fetchone() is None
        reconcile_pending_delete_intents(conn)
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert conn.execute("SELECT id FROM audit_log WHERE action='deleted'").fetchone() is None

    def test_keep_landing_after_the_arr_delete_started_does_not_refuse(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        def keep_lands_during_delete(_id):
            insert_scheduled_action(
                get_db(), media_item_id="m1", action="protected_forever", token="tok1"
            )

        radarr.delete_movie.side_effect = keep_lands_during_delete

        with patch(RADARR, return_value=radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        assert conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone() is None
        assert (
            conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m1'").fetchone()
            is None
        )
        assert conn.execute("SELECT id FROM audit_log WHERE action='deleted'").fetchone()


class TestMediaDeleteTransactional:
    """Transactional delete with Arr failure propagation."""

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    def test_arr_failure_returns_502_and_preserves_row(self, app_factory, authed_client, conn):
        """If Radarr delete raises, the endpoint returns 502 and keeps the DB row."""
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=0, body_snippet="transport error: ConnectionError", url="http://radarr/"
        )

        with patch(RADARR, return_value=mock_radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        assert _row_exists(conn, "m1")

    def _assert_not_booked_deleted(self, conn, media_id: str) -> None:
        assert _row_exists(conn, media_id)
        assert "deleted" not in _audit_actions(conn, media_id)
        assert conn.execute("SELECT COUNT(*) FROM deleted_media").fetchone()[0] == 0
        intent = conn.execute(
            "SELECT completed_at, last_error FROM delete_intents WHERE media_item_id = ?",
            (media_id,),
        ).fetchone()
        assert intent["completed_at"] and intent["last_error"]

    def test_radarr_404_on_the_delete_is_not_booked_as_deleted(
        self, app_factory, authed_client, conn
    ):
        """A 404 only says the entry left Radarr; its files may still be on disk."""
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = SafeHTTPError(
            status_code=404, body_snippet="Not Found", url="http://radarr/"
        )

        with patch(RADARR, return_value=mock_radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 409
        assert "Radarr no longer has this movie" in resp.json()["message"]
        self._assert_not_booked_deleted(conn, "m1")

    def test_sonarr_404_for_the_show_during_the_delete_is_not_booked_as_deleted(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = _sonarr()
        mock_sonarr.delete_episode_files.side_effect = SafeHTTPError(
            status_code=404, body_snippet="Not Found", url="http://sonarr/"
        )

        with patch(SONARR, return_value=mock_sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 409
        assert "Sonarr no longer has this show" in resp.json()["message"]
        self._assert_not_booked_deleted(conn, "s1")

    def test_season_files_sonarr_still_lists_after_the_delete_are_not_booked_as_deleted(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = _sonarr()
        mock_sonarr.delete_episode_files.side_effect = None

        with patch(SONARR, return_value=mock_sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 409
        mock_sonarr.delete_series.assert_not_called()
        self._assert_not_booked_deleted(conn, "s1")

    def test_an_episode_upgraded_during_the_delete_is_not_booked_as_deleted(
        self, app_factory, authed_client, conn
    ):
        _insert_tv_season(conn)
        client = authed_client(_app(app_factory, conn), conn)
        mock_sonarr = _sonarr()
        verified = mock_sonarr.get_episode_files.side_effect
        upgrade = {"id": 9, "seasonNumber": 1, "relativePath": "Season 01/E02.mkv", "size": 9}
        listings = {"n": 0}

        def upgrade_lands_after_the_check(series_id):
            listings["n"] += 1
            if listings["n"] == 1:
                return verified(series_id)
            return [f for f in verified(series_id) if f["id"] != 2] + [upgrade]

        mock_sonarr.get_episode_files.side_effect = upgrade_lands_after_the_check

        with patch(SONARR, return_value=mock_sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 409
        mock_sonarr.delete_episode_files.assert_called_once_with([1, 2])
        mock_sonarr.unmonitor_season.assert_not_called()
        mock_sonarr.delete_series.assert_not_called()
        self._assert_not_booked_deleted(conn, "s1")

    def test_retry_after_failure_succeeds(self, app_factory, authed_client, conn):
        """After a transient Arr failure, a retry must succeed (idempotency)."""
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = [
            SafeHTTPError(
                status_code=0, body_snippet="transport error: ConnectionError", url="http://radarr/"
            ),
            None,
        ]

        with patch(RADARR, return_value=mock_radarr):
            first = client.post("/api/media/m1/delete")
            assert first.status_code == 502
            second = client.post("/api/media/m1/delete")
            assert second.status_code == 200

        assert not _row_exists(conn, "m1")


class TestMediaDeleteLocalFiles:
    """A file mediaman can see on disk that outlives the Radarr/Sonarr delete is never booked."""

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    @staticmethod
    def _on_disk(path) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return {"path": str(path), "size": 1_000_000}

    def _assert_not_booked(self, resp, conn, media_id: str) -> None:
        assert resp.status_code == 409
        assert resp.json()["error"] == "delete_unconfirmed"
        assert _row_exists(conn, media_id)
        assert "deleted" not in _audit_actions(conn, media_id)
        assert conn.execute("SELECT COUNT(*) FROM deleted_media").fetchone()[0] == 0
        intent = conn.execute(
            "SELECT completed_at, last_error FROM delete_intents WHERE media_item_id = ?",
            (media_id,),
        ).fetchone()
        assert intent["completed_at"] and intent["last_error"]

    def test_a_curated_copy_left_on_disk_after_the_radarr_delete_is_not_booked(
        self, app_factory, authed_client, conn, tmp_path
    ):
        kids = self._on_disk(tmp_path / "Kids/Test Movie (2020)/Test Movie (2020).mkv")
        _insert_movie(conn, tracked_files=[kids])
        client = authed_client(_app(app_factory, conn), conn)
        radarr = _radarr()

        with (
            patch(RADARR, return_value=radarr),
            patch.object(delete_route, "_LOCAL_GONE_WAIT_SECONDS", 0),
        ):
            resp = client.post("/api/media/m1/delete")

        radarr.delete_movie.assert_called_once_with(101)
        self._assert_not_booked(resp, conn, "m1")

    def test_a_movie_file_radarr_removes_in_the_background_is_booked(
        self, app_factory, authed_client, conn, tmp_path
    ):
        movie = tmp_path / "movies/Test Movie (2020)/Test Movie (2020).mkv"
        _insert_movie(conn, tracked_files=[self._on_disk(movie)])
        client = authed_client(_app(app_factory, conn), conn)

        with (
            patch(RADARR, return_value=_radarr()),
            patch.object(delete_route.time, "sleep", side_effect=lambda _s: movie.unlink()),
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        assert not _row_exists(conn, "m1")

    def test_a_season_file_left_on_disk_after_the_sonarr_delete_is_not_booked(
        self, app_factory, authed_client, conn, tmp_path
    ):
        season = tmp_path / "tv/The Show/Season 01"
        tracked = [self._on_disk(season / f["path"].rsplit("/", 1)[1]) for f in SEASON_FILES]
        for entry, original in zip(tracked, SEASON_FILES, strict=True):
            entry["size"] = original["size"]
        _insert_tv_season(conn, tracked_files=tracked)
        client = authed_client(_app(app_factory, conn), conn)
        sonarr = _sonarr()

        with (
            patch(SONARR, return_value=sonarr),
            patch.object(delete_route, "_LOCAL_GONE_WAIT_SECONDS", 0),
        ):
            resp = client.post("/api/media/s1/delete")

        sonarr.delete_episode_files.assert_called_once_with([1, 2])
        self._assert_not_booked(resp, conn, "s1")


class TestMediaDeleteBareRequestsException:
    """Bare requests.RequestException from the Arr client must be caught gracefully.

    SafeHTTPClient wraps retryable transport errors into SafeHTTPError, but a
    non-retryable requests.RequestException (e.g. requests.ConnectionError raised
    directly by the client) can still propagate bare out of ArrClient._get/_post.
    The delete route must catch it, call _fail_delete_intent (so the intent is not
    left stranded without an error record), and return 502 — NOT an unhandled 500.
    """

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()

    def test_bare_connection_error_returns_502_and_records_error_on_intent(
        self, app_factory, authed_client, conn
    ):
        """requests.ConnectionError from Radarr → 502, row preserved, intent has last_error."""
        _insert_movie(conn, radarr_id=101)
        client = authed_client(_app(app_factory, conn), conn)

        mock_radarr = _radarr()
        mock_radarr.delete_movie.side_effect = requests.ConnectionError("connection refused")

        with patch(RADARR, return_value=mock_radarr):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        assert resp.json()["ok"] is False

        # DB row must be preserved — delete was not confirmed
        assert _row_exists(conn, "m1")

        # Intent must have been recorded and have last_error set (not silently stranded)
        intents = conn.execute(
            "SELECT last_error FROM delete_intents WHERE media_item_id = 'm1'"
        ).fetchall()
        assert intents, "no delete intent was recorded"
        assert any(r["last_error"] for r in intents), (
            "_fail_delete_intent was not called — intent stranded without error record"
        )

    def test_bare_connection_error_sonarr_returns_502_and_records_error_on_intent(
        self, app_factory, authed_client, conn
    ):
        """requests.ConnectionError from Sonarr → 502, row preserved, intent has last_error."""
        _insert_tv_season(conn, sonarr_id=202, season=1)
        client = authed_client(_app(app_factory, conn), conn)

        mock_sonarr = _sonarr()
        mock_sonarr.delete_episode_files.side_effect = requests.ConnectionError("refused")

        with patch(SONARR, return_value=mock_sonarr):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 502
        assert resp.json()["ok"] is False

        assert _row_exists(conn, "s1")

        intents = conn.execute(
            "SELECT last_error FROM delete_intents WHERE media_item_id = 's1'"
        ).fetchall()
        assert intents, "no delete intent was recorded"
        assert any(r["last_error"] for r in intents), (
            "_fail_delete_intent was not called — intent stranded without error record"
        )


class TestMediaKeep:
    def test_keep_requires_auth(self, app_factory, conn):
        """Keep endpoint returns 401 without a session cookie."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/media/m1/keep", data={"duration": "30 days"})
        assert resp.status_code == 401

    def test_keep_forever_inserts_protected_forever_action(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "forever"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        sa = conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "protected_forever"

    def test_keep_30d_inserts_snoozed_action(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "30 days"})

        assert resp.status_code == 200

        sa = conn.execute(
            "SELECT action, snooze_duration FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "snoozed"
        assert sa["snooze_duration"] == "30 days"

    def test_keep_invalid_duration_returns_400(self, app_factory, authed_client, conn):
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "1y"})
        assert resp.status_code == 400

    def test_keep_nonexistent_item_returns_404(self, app_factory, authed_client, conn):
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/no-such-id/keep", data={"duration": "7 days"})
        assert resp.status_code == 404

    def test_keep_updates_existing_action(self, app_factory, authed_client, conn):
        """Keep on an already-kept item updates the existing row rather than inserting a duplicate."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        client.post("/api/media/m1/keep", data={"duration": "7 days"})
        resp = client.post("/api/media/m1/keep", data={"duration": "forever"})

        assert resp.status_code == 200
        rows = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchall()
        # Must be at most one active row
        assert len(rows) == 1
        sa = conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa["action"] == "protected_forever"

    def test_keep_while_the_item_is_being_deleted_is_saved_and_says_so(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="scheduled_deletion",
            token="tok-m1",
            token_used=False,
            delete_status="deleting",
        )
        client = authed_client(_app(app_factory, conn), conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "forever"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deletion_running"] is True
        assert "already being deleted" in body["message"]
        rows = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("protected_forever", "deleting")]

    def test_keep_of_an_item_not_being_deleted_carries_no_warning(
        self, app_factory, authed_client, conn
    ):
        _insert_movie(conn)
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="scheduled_deletion",
            token="tok-m1",
            token_used=False,
            delete_status="pending",
        )
        client = authed_client(_app(app_factory, conn), conn)

        body = client.post("/api/media/m1/keep", data={"duration": "forever"}).json()

        assert body["deletion_running"] is False
        assert "message" not in body

    def test_keep_during_a_removal_that_succeeds_says_the_files_may_be_gone(
        self, app_factory, authed_client, conn, db_path, tmp_path, monkeypatch
    ):
        from datetime import UTC, datetime, timedelta

        from mediaman.db import init_db
        from mediaman.scanner import deletions

        root = tmp_path / "movies"
        root.mkdir()
        film = root / "Film.mkv"
        film.write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m1", plex_rating_key="rk1", file_path=str(film))
        _set_ids(conn, "m1", tracked_files=[{"path": str(film), "size": 1}])
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="scheduled_deletion",
            token="tok-m1",
            execute_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            token_used=False,
        )
        client = authed_client(_app(app_factory, conn), conn)
        real = deletions.delete_tracked_files
        responses = []

        def remove(tracked_files, **kwargs):
            responses.append(client.post("/api/media/m1/keep", data={"duration": "forever"}))
            return real(tracked_files, **kwargs)

        monkeypatch.setattr(deletions, "delete_tracked_files", remove)

        deletions.DeletionExecutor(conn=init_db(str(db_path))).execute()

        assert [r.status_code for r in responses] == [200]
        assert "may already be gone" in responses[0].json()["message"]
        assert not film.exists()
        assert conn.execute("SELECT 1 FROM scheduled_actions").fetchone() is None
        audits = [
            r["action"]
            for r in conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'")
        ]
        assert audits == ["snoozed", "deleted"]


class TestMediaRedownload:
    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _REDOWNLOAD_LIMITER.reset()

    @pytest.fixture
    def post(self, app_factory, authed_client, conn):
        client = authed_client(_app(app_factory, conn), conn)

        def send(payload, *, radarr=None, sonarr=None):
            with patch(RADARR, return_value=radarr), patch(SONARR, return_value=sonarr):
                return client.post("/api/media/redownload", json=payload)

        return send

    @staticmethod
    def _radarr_without_movie() -> MagicMock:
        client = MagicMock()
        client.get_movie_by_tmdb.return_value = None
        return client

    @staticmethod
    def _sonarr_without_series() -> MagicMock:
        client = MagicMock()
        client.get_series_by_tvdb.return_value = None
        return client

    def test_redownload_requires_auth(self, app_factory, conn):
        client = TestClient(_app(app_factory, conn), raise_server_exceptions=True)
        assert client.post("/api/media/redownload", json={"title": "Dune"}).status_code == 401

    def test_the_buttons_movie_payload_is_accepted(self, post):
        radarr = self._radarr_without_movie()
        resp = post({"title": "Dune", "tmdb_id": 42}, radarr=radarr)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_the_buttons_season_payload_is_accepted(self, post):
        sonarr = self._sonarr_without_series()
        resp = post({"title": "Severance", "tvdb_id": 999, "season_number": 2}, sonarr=sonarr)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"title": "Dune"},
            {"title": "Dune", "season_number": 1},
            {"title": "Severance", "tvdb_id": 999},
            {"title": "Dune", "tmdb_id": 42, "tvdb_id": 999, "season_number": 1},
        ],
    )
    def test_a_request_without_a_usable_id_is_refused_with_400(self, post, payload):
        radarr, sonarr = MagicMock(), MagicMock()
        resp = post(payload, radarr=radarr, sonarr=sonarr)
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        assert radarr.method_calls == [] and sonarr.method_calls == []

    def test_empty_title_is_refused(self, post):
        assert post({"title": "   ", "tmdb_id": 42}, radarr=MagicMock()).status_code == 400

    def test_unknown_fields_are_still_refused(self, post):
        assert post({"title": "Dune", "tmdb_id": 42, "year": 2021}).status_code == 422

    def test_a_movie_radarr_still_holds_is_remonitored_not_added(self, post, conn):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 101, "tmdbId": 42}
        resp = post({"title": "Dune", "tmdb_id": 42}, radarr=radarr)
        assert resp.json()["ok"] is True
        radarr.remonitor_movie.assert_called_once_with(101)
        radarr.add_movie.assert_not_called()
        assert conn.execute("SELECT 1 FROM audit_log WHERE action='re_downloaded'").fetchone()

    def test_a_movie_radarr_no_longer_has_is_added(self, post):
        radarr = self._radarr_without_movie()
        resp = post({"title": "Dune", "tmdb_id": 42}, radarr=radarr)
        assert resp.json()["ok"] is True
        radarr.add_movie.assert_called_once_with(42, "Dune")
        radarr.remonitor_movie.assert_not_called()

    def test_a_season_sonarr_still_holds_is_remonitored_not_added(self, post, conn):
        sonarr = MagicMock()
        sonarr.get_series_by_tvdb.return_value = {"id": 202, "tvdbId": 999}
        resp = post({"title": "Severance", "tvdb_id": 999, "season_number": 2}, sonarr=sonarr)
        assert resp.json()["ok"] is True
        sonarr.remonitor_season.assert_called_once_with(202, 2)
        sonarr.add_series_with_seasons.assert_not_called()
        assert conn.execute("SELECT 1 FROM audit_log WHERE action='re_downloaded'").fetchone()

    def test_a_series_sonarr_no_longer_has_is_added_with_only_that_season(self, post):
        sonarr = self._sonarr_without_series()
        resp = post({"title": "Severance", "tvdb_id": 999, "season_number": 2}, sonarr=sonarr)
        assert resp.json()["ok"] is True
        sonarr.add_series_with_seasons.assert_called_once_with(999, "Severance", [2], [2])
        sonarr.remonitor_season.assert_not_called()

    @pytest.mark.parametrize("held", [True, False], ids=["remonitored", "added"])
    def test_a_season_redownload_is_audited_under_the_token_the_dashboard_matches(
        self, post, conn, held
    ):
        sonarr = MagicMock()
        sonarr.get_series_by_tvdb.return_value = {"id": 202, "tvdbId": 999} if held else None
        post({"title": "Severance", "tvdb_id": 999, "season_number": 2}, sonarr=sonarr)
        rows = conn.execute(
            "SELECT media_item_id FROM audit_log WHERE action = 're_downloaded'"
        ).fetchall()
        assert [r["media_item_id"] for r in rows] == ["tvdb:999:s2"]

    def test_a_movie_request_without_radarr_is_refused(self, post):
        resp = post({"title": "Dune", "tmdb_id": 42}, radarr=None, sonarr=MagicMock())
        assert resp.status_code == 409
        assert resp.json()["ok"] is False

    def test_overlong_title_is_truncated_not_rejected(self, post):
        radarr = self._radarr_without_movie()
        resp = post({"title": "A" * 300, "tmdb_id": 42}, radarr=radarr)
        assert resp.status_code == 200
        assert len(radarr.add_movie.call_args[0][1]) == 256


class TestRedownloadAlreadyAdded:
    """A duplicate refusal that races the lookup surfaces as 'already exists', not a failure."""

    @pytest.fixture(autouse=True)
    def _reset_limiter(self):
        _REDOWNLOAD_LIMITER.reset()

    def _post_movie(self, app_factory, authed_client, conn, error):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = None
        radarr.add_movie.side_effect = error
        client = authed_client(_app(app_factory, conn), conn)
        with patch(RADARR, return_value=radarr):
            return client.post("/api/media/redownload", json={"title": "Dune", "tmdb_id": 42})

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (409, "conflict"),
            (422, "unprocessable"),
            (400, '[{"errorMessage":"This movie has already been added"}]'),
        ],
    )
    def test_movie_duplicate_says_already_exists(
        self, app_factory, authed_client, conn, status, body
    ):
        err = SafeHTTPError(status_code=status, body_snippet=body, url="http://radarr/api/v3/movie")
        resp = self._post_movie(app_factory, authed_client, conn, err)
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "already exists in Radarr" in resp.json()["error"]

    def test_series_duplicate_says_already_exists(self, app_factory, authed_client, conn):
        sonarr = MagicMock()
        sonarr.get_series_by_tvdb.return_value = None
        sonarr.add_series_with_seasons.side_effect = SafeHTTPError(
            status_code=400,
            body_snippet="This series has already been added",
            url="http://sonarr/api/v3/series",
        )
        client = authed_client(_app(app_factory, conn), conn)
        with patch(SONARR, return_value=sonarr):
            resp = client.post(
                "/api/media/redownload",
                json={"title": "Severance", "tvdb_id": 999, "season_number": 1},
            )
        assert resp.json()["ok"] is False
        assert "already exists in Sonarr" in resp.json()["error"]

    def test_an_unrelated_400_is_a_failure_not_a_duplicate(self, app_factory, authed_client, conn):
        err = SafeHTTPError(
            status_code=400, body_snippet="Invalid root folder", url="http://radarr/api/v3/movie"
        )
        resp = self._post_movie(app_factory, authed_client, conn, err)
        assert resp.json()["ok"] is False
        assert "already exists" not in resp.json()["error"]

    def test_a_connection_error_is_reported_not_a_500(self, app_factory, authed_client, conn):
        resp = self._post_movie(app_factory, authed_client, conn, requests.ConnectionError("down"))
        assert resp.status_code == 200
        assert "connectivity" in resp.json()["error"].lower()


class TestMediaKeepRateLimit:
    """/api/media/{id}/keep must be rate-limited."""

    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _KEEP_LIMITER.reset()
        _DELETE_LIMITER.reset()

    def test_keep_rate_limit_blocks_after_window_exceeded(self, app_factory, authed_client, conn):
        """Hammering /keep more than 60 times per minute returns 429."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        cap = _KEEP_LIMITER._max_in_window
        for i in range(cap):
            r = client.post("/api/media/m1/keep", data={"duration": "forever"})
            assert r.status_code != 429, f"Rate limit fired early on iteration {i}"

        r = client.post("/api/media/m1/keep", data={"duration": "forever"})
        assert r.status_code == 429
        assert r.json()["error"] is not None


class TestLibrarySearchLikeEscape:
    """LIKE metacharacters in the search query must not be treated as wildcards."""

    def test_percent_sign_is_treated_as_literal(self, conn):
        """A query containing '%' must only match titles containing a literal '%'."""
        from mediaman.db import set_connection

        set_connection(conn)
        insert_media_item(
            conn,
            id="m1",
            title="50% Off",
            plex_rating_key="rk1",
            file_path="/f1",
            file_size_bytes=1_000_000,
        )
        insert_media_item(
            conn,
            id="m2",
            title="Normal Movie",
            plex_rating_key="rk2",
            file_path="/f2",
            file_size_bytes=1_000_000,
        )

        from mediaman.web.repository.library_query import fetch_library as _fetch_library

        items, _total = _fetch_library(conn, q="%")
        titles = {i["title"] for i in items}
        assert "50% Off" in titles
        assert "Normal Movie" not in titles

    def test_underscore_is_treated_as_literal(self, conn):
        """A query containing '_' must match titles with a literal underscore only."""
        from mediaman.db import set_connection

        set_connection(conn)
        insert_media_item(
            conn,
            id="m1",
            title="foo_bar",
            plex_rating_key="rk1",
            file_path="/f1",
            file_size_bytes=1_000_000,
        )
        insert_media_item(
            conn,
            id="m2",
            title="fooXbar",
            plex_rating_key="rk2",
            file_path="/f2",
            file_size_bytes=1_000_000,
        )

        from mediaman.web.repository.library_query import fetch_library as _fetch_library

        items, _total = _fetch_library(conn, q="_")
        titles = {i["title"] for i in items}
        assert "foo_bar" in titles
        assert "fooXbar" not in titles
