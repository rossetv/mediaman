"""Behaviour tests for review-wave fixes applied to the newsletter service.

Each test class is named after the finding ID it exercises, with a brief
description of the correctness property being verified.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from tests.helpers.factories import (
    insert_audit_log,
    insert_media_item,
    insert_scheduled_action,
    insert_subscriber,
)

_SECRET_KEY = "0123456789abcdef" * 4
_BASE_URL = "http://mediaman.local"
_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_DELETED_AT = (_NOW - timedelta(days=2)).isoformat()
_REDOWNLOAD_AT = (_NOW - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# F-01 — redownload index key mismatch
# ---------------------------------------------------------------------------


class TestF01RedownloadFilterWorks:
    """F-01: _build_redownload_index must key by media_item_id / plex_rating_key
    so that re-downloaded items are correctly excluded from the deleted-items list.
    """

    def test_redownloaded_item_excluded_from_deleted_list(self, db_path):
        """An item deleted then re-downloaded after the deletion must not appear
        in the newsletter's recently-deleted section."""
        conn = init_db(str(db_path))

        plex_rk = "rk-movie-99"
        insert_media_item(
            conn,
            id=plex_rk,
            title="Redownloaded Film",
            plex_rating_key=plex_rk,
            file_path="/media/film.mkv",
            file_size_bytes=1_000_000,
            media_type="movie",
        )
        # Deletion happened two days ago.
        insert_audit_log(
            conn,
            media_item_id=plex_rk,
            action="deleted",
            space_reclaimed_bytes=1_000_000,
            created_at=_DELETED_AT,
        )
        # Re-download happened one day ago (after deletion).
        insert_audit_log(
            conn,
            media_item_id=plex_rk,
            action="re_downloaded",
            space_reclaimed_bytes=0,
            created_at=_REDOWNLOAD_AT,
        )

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)

        titles = [i["title"] for i in items]
        assert "Redownloaded Film" not in titles, (
            "Re-downloaded item must be excluded from the deleted-items newsletter section"
        )

    def test_not_redownloaded_item_included_in_deleted_list(self, db_path):
        """An item deleted but NOT re-downloaded must still appear in the list."""
        conn = init_db(str(db_path))

        plex_rk = "rk-movie-100"
        insert_media_item(
            conn,
            id=plex_rk,
            title="Still Gone Film",
            plex_rating_key=plex_rk,
            file_path="/media/film2.mkv",
            file_size_bytes=500_000,
            media_type="movie",
        )
        insert_audit_log(
            conn,
            media_item_id=plex_rk,
            action="deleted",
            space_reclaimed_bytes=500_000,
            created_at=_DELETED_AT,
        )

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)

        titles = [i["title"] for i in items]
        assert "Still Gone Film" in titles, (
            "Item deleted but not re-downloaded must remain in the newsletter"
        )

    def test_redownload_before_deletion_does_not_exclude_item(self, db_path):
        """If the re-download happened BEFORE the deletion, the item should still appear
        (it was re-deleted after the re-download)."""
        conn = init_db(str(db_path))

        plex_rk = "rk-movie-101"
        earlier_redownload = (_NOW - timedelta(days=5)).isoformat()
        insert_media_item(
            conn,
            id=plex_rk,
            title="Deleted After Redownload",
            plex_rating_key=plex_rk,
            file_path="/media/film3.mkv",
            file_size_bytes=800_000,
            media_type="movie",
        )
        # Re-download happened 5 days ago.
        insert_audit_log(
            conn,
            media_item_id=plex_rk,
            action="re_downloaded",
            space_reclaimed_bytes=0,
            created_at=earlier_redownload,
        )
        # Then deleted again 2 days ago — AFTER the re-download.
        insert_audit_log(
            conn,
            media_item_id=plex_rk,
            action="deleted",
            space_reclaimed_bytes=800_000,
            created_at=_DELETED_AT,
        )

        from mediaman.services.mail.newsletter.summary import _load_deleted_items

        items = _load_deleted_items(conn, _SECRET_KEY, _BASE_URL, _NOW)

        titles = [i["title"] for i in items]
        assert "Deleted After Redownload" in titles, (
            "Item re-downloaded before its deletion timestamp must still appear"
        )


# ---------------------------------------------------------------------------
# F-07 — empty recipients list must not fall through to full subscriber query
# ---------------------------------------------------------------------------


class TestF07EmptyRecipientsNotFallthrough:
    """F-07: passing recipients=[] must send to nobody, not to all subscribers."""

    _PATCH_STORAGE = "mediaman.services.infra.storage.get_aggregate_disk_usage"
    _PATCH_MAILGUN = "mediaman.services.mail.mailgun.MailgunClient"
    _PATCH_RADARR = "mediaman.services.arr.build.build_radarr_from_db"
    _PATCH_SONARR = "mediaman.services.arr.build.build_sonarr_from_db"

    def _configure_mailgun(self, conn) -> None:
        now = datetime.now(UTC).isoformat()
        for key, value in (
            ("mailgun_domain", "mg.example.com"),
            ("mailgun_api_key", "key-testvalue"),
            ("mailgun_from_address", "media@example.com"),
            ("base_url", "http://mediaman.local"),
        ):
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (key, value, now),
            )
        conn.commit()

    def _add_scheduled_item(self, conn) -> None:
        insert_media_item(
            conn,
            id="mi-f07",
            title="F07 Test Movie",
            plex_rating_key="rk-f07",
            file_path="/media/f07.mkv",
            file_size_bytes=1_000_000,
        )
        insert_scheduled_action(
            conn,
            media_item_id="mi-f07",
            action="scheduled_deletion",
            token="tok-f07",
            token_used=False,
            notified=False,
        )

    @pytest.fixture(autouse=True)
    def _patch_boundaries(self):
        with (
            patch(
                self._PATCH_STORAGE,
                return_value={
                    "total_bytes": 10 << 40,
                    "used_bytes": 5 << 40,
                    "free_bytes": 5 << 40,
                },
            ),
            patch(self._PATCH_RADARR, return_value=None),
            patch(self._PATCH_SONARR, return_value=None),
        ):
            yield

    def test_empty_recipients_list_sends_to_nobody(self, db_path):
        """recipients=[] must result in zero sends, not a send to all subscribers."""
        from mediaman.services.mail.newsletter import send_newsletter

        conn = init_db(str(db_path))
        self._configure_mailgun(conn)
        # Add a real subscriber who must NOT receive the email.
        insert_subscriber(conn, email="should-not-receive@example.com")
        self._add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        with patch(self._PATCH_MAILGUN, return_value=mock_mailgun_instance):
            send_newsletter(
                conn,
                _SECRET_KEY,
                recipients=[],  # explicit empty — send to nobody
                mark_notified=False,
            )

        mock_mailgun_instance.send.assert_not_called()

    def test_none_recipients_sends_to_all_subscribers(self, db_path):
        """recipients=None (default) sends to all active subscribers as before."""
        from mediaman.services.mail.newsletter import send_newsletter

        conn = init_db(str(db_path))
        self._configure_mailgun(conn)
        insert_subscriber(conn, email="subscriber@example.com")
        self._add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        with patch(self._PATCH_MAILGUN, return_value=mock_mailgun_instance):
            send_newsletter(conn, _SECRET_KEY, recipients=None)

        mock_mailgun_instance.send.assert_called_once()
        assert mock_mailgun_instance.send.call_args.kwargs["to"] == "subscriber@example.com"
