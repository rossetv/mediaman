"""Tests for :mod:`mediaman.web.routes.recommended.api`.

Covers share-token minting and related guards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.routes.recommended.api import router as rec_router
from tests.helpers.factories import insert_suggestion

_SECRET = "a" * 64


def _make_rec_app(conn) -> TestClient:
    """Stand up a recommended-api app with admin always authenticated."""
    app = FastAPI()
    app.include_router(rec_router)
    app.state.config = Config(secret_key=_SECRET)
    app.state.db = conn
    set_connection(conn)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Finding 15: Refuse to mint public download token without TMDB id
# ---------------------------------------------------------------------------


class TestFinding15MintRequiresTmdbId:
    """Finding 15: share-token mint must refuse recommendations without a TMDB id."""

    def test_mint_without_tmdb_id_returns_422(self, conn):
        """Minting a share token for a suggestion without tmdb_id must return 422."""
        sid = insert_suggestion(conn, title="Test Movie", tmdb_id=None)
        client = _make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        assert resp.status_code == 422
        body = resp.json()
        assert not body.get("ok")
        assert "TMDB" in body.get("error", "")

    def test_mint_with_tmdb_id_would_succeed_if_base_url_set(self, conn):
        """Minting with a tmdb_id present does not fail on the identifier check."""
        sid = insert_suggestion(conn, title="Test Movie", tmdb_id=12345)
        client = _make_rec_app(conn)
        resp = client.post(f"/api/recommended/{sid}/share-token")
        # Should NOT return 422; may return 200 or other error (e.g. missing base_url)
        assert resp.status_code != 422


# ---------------------------------------------------------------------------
# Finding 15 (H-1): newsletter must skip redownload mint when no tmdb_id
# ---------------------------------------------------------------------------


class TestFinding15NewsletterSkipsMintWithoutTmdb:
    """Finding 15 (H-1): newsletter must skip redownload mint when no tmdb_id.

    The previous code hardcoded ``tmdb_id=None`` for deleted items, producing
    a public token whose submit fell back to ``lookup_by_term(title)``.  The
    fix is to omit the redownload URL entirely when the deleted item carries
    no stable identifier.  The template hides the button via
    ``{% if item.redownload_url %}``.
    """

    def test_deleted_item_without_tmdb_has_empty_redownload_url(self):
        from mediaman.services.mail.newsletter.recipients import _send_to_recipients

        captured: dict = {}

        class _FakeTemplate:
            def render(self, **kwargs):
                captured.update(kwargs)
                return "<html></html>"

        mailgun_stub = MagicMock()
        mailgun_stub.send_message.return_value = True

        deleted_no_tmdb = [{"title": "Ambiguous Title", "media_type": "movie"}]
        deleted_with_tmdb = [{"title": "Specific Film", "media_type": "movie", "tmdb_id": 12345}]

        _send_to_recipients(
            recipient_emails=["dest@example.com"],
            scheduled_items=[],
            deleted_items=deleted_no_tmdb + deleted_with_tmdb,
            this_week_items=[],
            storage={"total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "by_type": {}},
            reclaimed_week=0,
            reclaimed_month=0,
            reclaimed_total=0,
            subject="x",
            base_url="https://mm.example.com",
            secret_key=_SECRET,
            dry_run=False,
            grace_days=7,
            template=_FakeTemplate(),
            mailgun=mailgun_stub,
            report_date="2026-05-01",
            conn=None,
        )

        rendered_deleted = captured["deleted_items"]
        assert len(rendered_deleted) == 2
        # No tmdb_id → no redownload link, regardless of base_url.
        assert rendered_deleted[0]["redownload_url"] == ""
        # With tmdb_id → token-bearing URL.
        assert rendered_deleted[1]["redownload_url"].startswith("https://mm.example.com/download/")
