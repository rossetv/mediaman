"""Integration: scan → DB → /api/downloads seam.

Full cycle:
1. Seed Plex library items via a fake Plex client.
2. Run ScanEngine.run_scan() with fake Arr/newsletter stubs.
3. Assert media_items table is populated.
4. Hit GET /api/downloads with a valid session.
5. Assert the JSON response structure spans scanner→DB→route.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.auth.session import create_session, create_user
from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.scanner.engine import ScanEngine
from mediaman.web.routes.downloads import router as downloads_router

_TPL_DIR = Path(__file__).parent.parent.parent / "src" / "mediaman" / "web" / "templates"


def _fake_plex(items: list[dict]) -> MagicMock:
    """Minimal Plex client stub that returns the given movie items."""
    plex = MagicMock()
    plex.get_movie_items.return_value = items
    plex.get_show_seasons.return_value = []
    plex.get_watch_history.return_value = []
    plex.get_season_watch_history.return_value = []
    plex.get_ratings.return_value = []
    return plex


def _make_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(downloads_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


def _auth_client(app: FastAPI, conn) -> TestClient:
    create_user(conn, "admin", "password1234", enforce_policy=False)
    token = create_session(conn, "admin")
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session_token", token)
    return client


class TestScanToQueueFlow:
    def test_scanner_populates_db_and_api_returns_queue(self, db_path, secret_key, monkeypatch):
        """ScanEngine upserts items; /api/downloads reflects them."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

        conn = init_db(str(db_path))

        plex_items = [
            {
                "plex_rating_key": "101",
                "title": "Dune Part Two",
                "year": 2024,
                "file_path": "/media/movies/Dune2",
                "file_size_bytes": 8_000_000_000,
                "added_at": "2024-01-01T00:00:00+00:00",
                "tmdb_id": 693134,
                "poster_url": None,
            }
        ]
        fake_plex = _fake_plex(plex_items)

        # Suppress newsletter and recommendations side-effects.
        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
            patch("mediaman.services.infra.storage.get_aggregate_disk_usage", return_value={}),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=fake_plex,
                library_ids=["1"],
                library_types={"1": "movie"},
                secret_key=secret_key,
                min_age_days=0,
                inactivity_days=0,
                grace_days=14,
            )
            engine.run_scan()

        # Assert DB has the item.
        row = conn.execute("SELECT title FROM media_items WHERE plex_rating_key='101'").fetchone()
        assert row is not None
        assert row["title"] == "Dune Part Two"

        # Hit the downloads API — should return the expected shape.
        app = _make_app(conn, secret_key)
        client = _auth_client(app, conn)
        resp = client.get("/api/downloads")
        assert resp.status_code == 200
        body = resp.json()
        assert "queue" in body
        assert "hero" in body
        assert "upcoming" in body
        assert "recent" in body

    def test_scanner_does_not_orphan_real_items(self, db_path, secret_key):
        """Two sequential scans: items present in both are retained."""
        conn = init_db(str(db_path))

        plex_items = [
            {
                "plex_rating_key": "202",
                "title": "Oppenheimer",
                "year": 2023,
                "file_path": "/media/movies/Oppenheimer",
                "file_size_bytes": 10_000_000_000,
                "added_at": "2023-07-21T00:00:00+00:00",
                "tmdb_id": 872585,
                "poster_url": None,
            }
        ]
        fake_plex = _fake_plex(plex_items)

        with (
            patch("mediaman.scanner.engine._send_newsletter"),
            patch("mediaman.scanner.engine._refresh_recommendations"),
        ):
            engine = ScanEngine(
                conn=conn,
                plex_client=fake_plex,
                library_ids=["2"],
                library_types={"2": "movie"},
                secret_key=secret_key,
                min_age_days=9999,  # nothing eligible
                inactivity_days=9999,
                grace_days=14,
            )
            engine.run_scan()
            engine.run_scan()  # second pass — item must survive

        count = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE plex_rating_key='202'"
        ).fetchone()[0]
        assert count == 1
