"""Integration: seed DB → send_newsletter → Mailgun called, notified flag set.

Exercises:
  scheduled_actions + subscribers tables
  → newsletter._load_scheduled_items / _load_recipients
  → MailgunClient.send (faked via fake_http)
  → notified=1 in scheduled_actions
  → send result logged

No internal mocking — only the Mailgun HTTP layer (external service) is faked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from mediaman.db import init_db
from mediaman.services.mail.newsletter import send_newsletter

_SECRET = "0123456789abcdef" * 4  # matches conftest fixture


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
        (key, value, _now()),
    )
    conn.commit()


def _configure_mailgun(conn) -> None:
    _insert_setting(conn, "mailgun_domain", "mg.example.com")
    _insert_setting(conn, "mailgun_api_key", "key-test")
    _insert_setting(conn, "mailgun_from_address", "media@example.com")
    _insert_setting(conn, "base_url", "http://mediaman.local")


def _seed_scheduled_item(conn, media_id: str = "mi1", title: str = "Test Movie") -> None:
    conn.execute(
        "INSERT INTO media_items "
        "(id, title, media_type, plex_library_id, plex_rating_key, added_at, file_path, file_size_bytes) "
        "VALUES (?, ?, 'movie', 1, ?, ?, '/media/test.mkv', 5000000000)",
        (media_id, title, media_id, _now()),
    )
    conn.execute(
        "INSERT INTO scheduled_actions "
        "(media_item_id, action, scheduled_at, token, token_used, notified) "
        "VALUES (?, 'scheduled_deletion', ?, 'tok-test', 0, 0)",
        (media_id, _now()),
    )
    conn.commit()


def _add_subscriber(conn, email: str = "viewer@example.com") -> None:
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
        (email, _now()),
    )
    conn.commit()


class TestNewsletterFlow:
    def test_newsletter_sends_and_marks_notified(self, db_path):
        """send_newsletter calls Mailgun once and flips notified=1 for the item."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _seed_scheduled_item(conn)
        _add_subscriber(conn)

        captured_calls = []

        class FakeMailgunClient:
            def __init__(self, *args, **kwargs):
                pass

            def send(self, *, to, subject, html):
                captured_calls.append({"to": to, "subject": subject})

        with (
            patch("mediaman.services.mail.mailgun.MailgunClient", FakeMailgunClient),
            patch(
                "mediaman.services.infra.storage.get_aggregate_disk_usage",
                return_value={
                    "total_bytes": 10 << 40,
                    "used_bytes": 5 << 40,
                    "free_bytes": 5 << 40,
                },
            ),
            patch("mediaman.services.arr.state.build_radarr_cache", return_value={}),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value={}),
        ):
            send_newsletter(conn, _SECRET)

        # Mailgun must have been called for the one subscriber.
        assert len(captured_calls) == 1
        assert captured_calls[0]["to"] == "viewer@example.com"

        # The scheduled item's notified flag must now be set.
        row = conn.execute(
            "SELECT notified FROM scheduled_actions WHERE media_item_id='mi1'"
        ).fetchone()
        assert row is not None
        assert row["notified"] == 1

    def test_newsletter_multiple_subscribers_all_receive(self, db_path):
        """Two active subscribers each get an email; both succeed."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _seed_scheduled_item(conn)
        _add_subscriber(conn, "alice@example.com")
        _add_subscriber(conn, "bob@example.com")

        recipients_sent = []

        class FakeMailgunClient:
            def __init__(self, *args, **kwargs):
                pass

            def send(self, *, to, subject, html):
                recipients_sent.append(to)

        with (
            patch("mediaman.services.mail.mailgun.MailgunClient", FakeMailgunClient),
            patch(
                "mediaman.services.infra.storage.get_aggregate_disk_usage",
                return_value={
                    "total_bytes": 10 << 40,
                    "used_bytes": 5 << 40,
                    "free_bytes": 5 << 40,
                },
            ),
            patch("mediaman.services.arr.state.build_radarr_cache", return_value={}),
            patch("mediaman.services.arr.state.build_sonarr_cache", return_value={}),
        ):
            send_newsletter(conn, _SECRET)

        assert set(recipients_sent) == {"alice@example.com", "bob@example.com"}

    def test_newsletter_skipped_when_no_content(self, db_path):
        """No scheduled items and no recommendations → newsletter not sent."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn)
        # No media items or scheduled actions seeded.

        send_calls = []

        class FakeMailgunClient:
            def __init__(self, *args, **kwargs):
                pass

            def send(self, *, to, subject, html):
                send_calls.append(to)

        with (
            patch("mediaman.services.mail.mailgun.MailgunClient", FakeMailgunClient),
            patch(
                "mediaman.services.infra.storage.get_aggregate_disk_usage",
                return_value={
                    "total_bytes": 10 << 40,
                    "used_bytes": 5 << 40,
                    "free_bytes": 5 << 40,
                },
            ),
        ):
            send_newsletter(conn, _SECRET)

        assert send_calls == [], "Newsletter must not be sent when there is nothing to report"
