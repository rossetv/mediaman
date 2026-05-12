"""Integration: scan → DB → /api/downloads seam.

Full cycle:
1. Seed Plex library items via a fake Plex client.
2. Run ScanEngine.run_scan() with fake Arr/newsletter stubs.
3. Assert media_items table is populated.
4. Hit GET /api/downloads with a valid session.
5. Assert the JSON response structure spans scanner→DB→route.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.scanner.engine import ScanEngine
from mediaman.web.routes.downloads import router as downloads_router


def _fake_plex(items: list[dict]) -> MagicMock:
    """Minimal Plex client stub that returns the given movie items."""
    plex = MagicMock()
    plex.get_movie_items.return_value = items
    plex.get_show_seasons.return_value = []
    plex.get_watch_history.return_value = []
    plex.get_season_watch_history.return_value = []
    plex.get_ratings.return_value = []
    return plex


class TestScanToQueueFlow:
    def test_scanner_populates_db_and_api_returns_queue(
        self, app_factory, authed_client, conn, db_path, secret_key, monkeypatch
    ):
        """ScanEngine upserts items; /api/downloads reflects them."""
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", secret_key)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(db_path.parent))

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
        app = app_factory(downloads_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/downloads")
        assert resp.status_code == 200
        body = resp.json()
        assert "queue" in body
        assert "hero" in body
        assert "upcoming" in body
        assert "recent" in body

    def test_scanner_does_not_orphan_real_items(self, conn, secret_key):
        """Two sequential scans: items present in both are retained."""
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
