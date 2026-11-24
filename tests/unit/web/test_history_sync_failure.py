"""A failed library sync records its error in the history without a Plex token."""

from __future__ import annotations

from unittest.mock import patch

from mediaman.web.routes.scan import router as scan_router


class TestSyncFailureRecord:
    def test_a_plex_token_in_the_error_is_not_stored(
        self, app_factory, authed_client, conn, db_path
    ):
        app = app_factory(scan_router, conn=conn, state_extras={"db_path": str(db_path)})
        client = authed_client(app, conn)
        with patch(
            "mediaman.scanner.runner.run_library_sync",
            side_effect=ValueError(
                "bad reply from http://plex:32400/library?X-Plex-Token=abcd1234"
            ),
        ):
            client.post("/api/library/sync")
        stored = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:library.sync.failed'"
        ).fetchone()["detail"]
        assert "abcd1234" not in stored
        assert "bad reply" in stored
