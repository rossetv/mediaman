"""Tests for send_newsletter() in mediaman.services.mail.newsletter."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import init_db
from mediaman.services.mail.newsletter import NewsletterConfigError, send_newsletter

_SECRET_KEY = "0123456789abcdef" * 4  # 64 hex chars — matches test conftest fixture


def _insert_setting(conn, key: str, value: str) -> None:
    """Insert a plaintext (encrypted=0) setting row."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, ?)",
        (key, value, now),
    )
    conn.commit()


def _configure_mailgun(conn) -> None:
    """Write the minimal Mailgun settings required for send_newsletter to proceed."""
    _insert_setting(conn, "mailgun_domain", "mg.example.com")
    _insert_setting(conn, "mailgun_api_key", "key-testvalue")
    _insert_setting(conn, "mailgun_from_address", "media@example.com")
    _insert_setting(conn, "base_url", "http://mediaman.local")


def _add_subscriber(conn, email: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
        (email, now),
    )
    conn.commit()


def _add_scheduled_item(conn) -> None:
    """Insert a media_item + scheduled_deletion action so the newsletter has content."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO media_items
           (id, title, media_type, plex_library_id, plex_rating_key,
            added_at, file_path, file_size_bytes)
           VALUES ('mi1', 'Test Movie', 'movie', 1, 'rk1', ?, '/media/test.mkv', 5000000000)""",
        (now,),
    )
    conn.execute(
        """INSERT INTO scheduled_actions
           (media_item_id, action, scheduled_at, token, token_used, notified)
           VALUES ('mi1', 'scheduled_deletion', ?, 'tok1', 0, 0)""",
        (now,),
    )
    conn.commit()


# Patch targets that live outside the isolated test DB
_PATCH_STORAGE = "mediaman.services.infra.storage.get_aggregate_disk_usage"
# MailgunClient is imported inside the function body from mediaman.services.mail.mailgun,
# so patch it at the source module.
_PATCH_MAILGUN = "mediaman.services.mail.mailgun.MailgunClient"
_PATCH_RADARR = "mediaman.services.arr.build.build_radarr_from_db"
_PATCH_SONARR = "mediaman.services.arr.build.build_sonarr_from_db"
_PATCH_RADARR_CACHE = "mediaman.services.arr.state.build_radarr_cache"
_PATCH_SONARR_CACHE = "mediaman.services.arr.state.build_sonarr_cache"


def _fake_disk():
    return {"total_bytes": 10 << 40, "used_bytes": 5 << 40, "free_bytes": 5 << 40}


class TestSendNewsletterNoMailgunConfig:
    def test_no_mailgun_config_sends_nothing(self, db_path):
        """When Mailgun is not configured at all, send_newsletter exits early without sending."""
        conn = init_db(str(db_path))
        _add_subscriber(conn, "alice@example.com")

        mock_mailgun_cls = MagicMock()
        with patch(_PATCH_MAILGUN, mock_mailgun_cls):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_cls.assert_not_called()

    def test_missing_from_address_raises_config_error(self, db_path):
        """When only from_address is missing, NewsletterConfigError is raised (CAN-SPAM / PECR)."""
        conn = init_db(str(db_path))
        _insert_setting(conn, "mailgun_domain", "mg.example.com")
        _insert_setting(conn, "mailgun_api_key", "key-testvalue")
        # from_address deliberately omitted
        _insert_setting(conn, "base_url", "http://mediaman.local")
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_cls = MagicMock()
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
            pytest.raises(NewsletterConfigError, match="mailgun_from_address"),
        ):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_cls.assert_not_called()

    def test_missing_base_url_raises_config_error(self, db_path):
        """When base_url is missing, NewsletterConfigError is raised (no unsubscribe URL)."""
        conn = init_db(str(db_path))
        _insert_setting(conn, "mailgun_domain", "mg.example.com")
        _insert_setting(conn, "mailgun_api_key", "key-testvalue")
        _insert_setting(conn, "mailgun_from_address", "media@example.com")
        # base_url deliberately omitted
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_cls = MagicMock()
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
            pytest.raises(NewsletterConfigError, match="base_url"),
        ):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_cls.assert_not_called()

    @pytest.mark.parametrize(
        "bad_url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "ftp://files.example.com",
            "//example.com",  # protocol-relative — also rejected
        ],
    )
    def test_non_http_base_url_raises_config_error(self, db_path, bad_url):
        """H70: base_url with a non-HTTP(S) scheme must be rejected before sending.

        A javascript: or data: base_url would produce XSS-grade href values in
        the newsletter HTML, so the service must refuse to proceed.
        """
        conn = init_db(str(db_path))
        _insert_setting(conn, "mailgun_domain", "mg.example.com")
        _insert_setting(conn, "mailgun_api_key", "key-testvalue")
        _insert_setting(conn, "mailgun_from_address", "media@example.com")
        _insert_setting(conn, "base_url", bad_url)
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_cls = MagicMock()
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
            pytest.raises(NewsletterConfigError, match="base_url"),
        ):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_cls.assert_not_called()

    def test_missing_domain_only_raises_config_error(self, db_path):
        """When only domain is missing (api_key present), NewsletterConfigError is raised."""
        conn = init_db(str(db_path))
        _insert_setting(conn, "mailgun_api_key", "key-testvalue")
        _insert_setting(conn, "mailgun_from_address", "media@example.com")
        _insert_setting(conn, "base_url", "http://mediaman.local")
        # mailgun_domain deliberately omitted
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_cls = MagicMock()
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
            pytest.raises(NewsletterConfigError, match="mailgun_domain"),
        ):
            send_newsletter(conn, _SECRET_KEY)


class TestSendNewsletterNoSubscribers:
    def test_no_subscribers_sends_nothing(self, db_path):
        """When there are no active subscribers, no email is sent even if Mailgun is configured."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_scheduled_item(conn)

        mock_mailgun_cls = MagicMock()
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_cls.assert_not_called()


class TestSendNewsletterWithScheduledItems:
    def test_scheduled_items_trigger_email_send(self, db_path):
        """When there are scheduled items and subscribers, email is sent."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)

        mock_mailgun_instance.send.assert_called_once()
        call_kwargs = mock_mailgun_instance.send.call_args
        assert call_kwargs.kwargs["to"] == "alice@example.com"
        assert "Test Movie" in call_kwargs.kwargs["html"]

    def test_scheduled_items_marked_notified_after_send(self, db_path):
        """After a successful send, scheduled_actions rows are marked notified=1."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)

        sa = conn.execute(
            "SELECT notified FROM scheduled_actions WHERE media_item_id='mi1'"
        ).fetchone()
        assert sa is not None
        assert sa["notified"] == 1

    def test_email_send_failure_does_not_crash(self, db_path):
        """When Mailgun raises, send_newsletter logs and continues without propagating."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn, "alice@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_instance.send.side_effect = RuntimeError("Mailgun is down")
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        # Should not raise
        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)

        # Send was attempted but failed — items must NOT be marked notified
        sa = conn.execute(
            "SELECT notified FROM scheduled_actions WHERE media_item_id='mi1'"
        ).fetchone()
        assert sa["notified"] == 0

    def test_multiple_subscribers_each_get_an_email(self, db_path):
        """Each active subscriber receives an individual email."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn, "alice@example.com")
        _add_subscriber(conn, "bob@example.com")
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(conn, _SECRET_KEY)

        assert mock_mailgun_instance.send.call_count == 2
        sent_to = {call.kwargs["to"] for call in mock_mailgun_instance.send.call_args_list}
        assert "alice@example.com" in sent_to
        assert "bob@example.com" in sent_to


class TestSendNewsletterRecipientOverride:
    def test_recipients_override_bypasses_subscriber_table(self, db_path):
        """When recipients= is provided, the subscriber table is ignored."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        # No subscribers in the DB
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(
                conn,
                _SECRET_KEY,
                recipients=["override@example.com"],
                mark_notified=False,
            )

        mock_mailgun_instance.send.assert_called_once()
        assert mock_mailgun_instance.send.call_args.kwargs["to"] == "override@example.com"

    def test_mark_notified_false_leaves_rows_unnotified(self, db_path):
        """With mark_notified=False the scheduled_actions rows stay at notified=0."""
        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_scheduled_item(conn)

        mock_mailgun_instance = MagicMock()
        mock_mailgun_cls = MagicMock(return_value=mock_mailgun_instance)

        with (
            patch(_PATCH_MAILGUN, mock_mailgun_cls),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
        ):
            send_newsletter(
                conn,
                _SECRET_KEY,
                recipients=["admin@example.com"],
                mark_notified=False,
            )

        sa = conn.execute(
            "SELECT notified FROM scheduled_actions WHERE media_item_id='mi1'"
        ).fetchone()
        assert sa["notified"] == 0


class TestNewsletterSuggestionContext:
    """H59: suggestion rows must be passed to the template as explicit dicts, not raw DB rows."""

    def test_suggestion_context_contains_only_known_fields(self, db_path):
        """Suggestion items in the template context must only contain the declared fields."""
        import jinja2

        conn = init_db(str(db_path))
        _configure_mailgun(conn)
        _add_subscriber(conn, "alice@example.com")

        now = datetime.now(timezone.utc).isoformat()
        # Insert a suggestion row with extra DB columns that must NOT leak into the template.
        conn.execute(
            """INSERT INTO suggestions (title, year, media_type, category, tmdb_id, imdb_id,
               description, reason, poster_url, trailer_url, rating, rt_rating,
               tagline, runtime, genres, cast_json, director, trailer_key,
               imdb_rating, metascore, batch_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "Test Film",
                2024,
                "movie",
                "trending",
                12345,
                "tt0000001",
                "A test film.",
                "Great reason",
                "https://example/poster.jpg",
                "https://youtube.com/watch?v=xyz",
                8.5,
                90,
                "A tagline",
                120,
                '["Action"]',
                '["Actor One"]',
                "Director One",
                "abckey",
                7.8,
                85,
                now[:10],
                now,
            ),
        )
        conn.commit()

        captured_context: dict = {}

        def capturing_render(self_or_ctx=None, **kwargs):
            # jinja2.Template.render is called as an instance method; the first
            # positional arg is ``self`` when used as an unbound patch.
            captured_context.update(kwargs)
            return "<html></html>"

        with (
            patch(_PATCH_MAILGUN, MagicMock(return_value=MagicMock())),
            patch(_PATCH_STORAGE, return_value=_fake_disk()),
            patch(_PATCH_RADARR, return_value=None),
            patch(_PATCH_SONARR, return_value=None),
            patch.object(jinja2.Template, "render", capturing_render),
        ):
            send_newsletter(conn, _SECRET_KEY)

        assert "this_week_items" in captured_context
        items = captured_context["this_week_items"]
        assert len(items) == 1
        item = items[0]

        # These are the only fields the template should receive.
        allowed_keys = {
            "id",
            "title",
            "media_type",
            "category",
            "description",
            "reason",
            "poster_url",
            "tmdb_id",
            "rating",
            "rt_rating",
            "download_url",
            "download_state",
        }
        extra_keys = set(item.keys()) - allowed_keys
        assert not extra_keys, f"Unexpected keys leaked into template context: {extra_keys}"
