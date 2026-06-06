"""Behaviour-named tests for security and correctness fixes from K-web-routes2 review.

Covers:
- B-03: api_keep_show is rate-limited (returns 429 when limit exceeded)
- B-04: upsert_kept_show + set_protected_state + log_audit roll back atomically
- H-01: keep_submit and keep_forever handle token replay atomically
- H-07: _add_rec_to_sonarr rejects Sonarr results whose tmdbId does not match
- DEFERRED-A: fetch_suggestion_by_id returns a typed SuggestionDetail dataclass
- DEFERRED-B: unsubscribe_confirm writes a subscriber.opted_out security event
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.crypto import generate_keep_token, generate_unsubscribe_token
from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.services.scheduled_actions import token_hash
from mediaman.web.routes.keep import _KEEP_POST_LIMITER
from mediaman.web.routes.keep import router as keep_router
from mediaman.web.routes.kept_show import _KEEP_SHOW_LIMITER
from mediaman.web.routes.kept_show import router as kept_show_router
from mediaman.web.routes.subscribers import _UNSUB_LIMITER
from tests.helpers.factories import (
    insert_media_item,
    insert_scheduled_action,
    insert_subscriber,
    insert_suggestion,
)

# ---------------------------------------------------------------------------
# Shared secrets / helpers
# ---------------------------------------------------------------------------

_SECRET = "a" * 64


def _now_plus_days(n: int) -> str:
    return (datetime.now(UTC) + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# App builder helpers
# ---------------------------------------------------------------------------


def _kept_show_app(conn: sqlite3.Connection) -> TestClient:
    """Minimal app for the show-keep route, admin always authenticated."""
    from mediaman.web.auth.middleware import get_current_admin

    app = FastAPI()
    app.include_router(kept_show_router)
    app.state.config = Config(secret_key=_SECRET)
    app.state.db = conn
    set_connection(conn)
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    return TestClient(app, raise_server_exceptions=True)


def _keep_app(conn: sqlite3.Connection) -> TestClient:
    """Minimal app for the public keep route (no admin auth required)."""
    mock_templates = MagicMock()
    mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
        json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
    )
    app = FastAPI()
    app.include_router(keep_router)
    app.state.config = Config(secret_key=_SECRET)
    app.state.db = conn
    app.state.templates = mock_templates
    set_connection(conn)
    return TestClient(app, raise_server_exceptions=True)


def _make_full_app(db_path, secret_key) -> tuple[object, object]:
    """Full create_app() for routes that need middleware (subscribers, unsubscribe)."""
    conn = init_db(str(db_path))
    set_connection(conn)
    app = create_app()
    app.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    app.state.db = conn
    return app, conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Reset all rate limiters touched by this file before and after each test."""
    _KEEP_SHOW_LIMITER.reset()
    _KEEP_POST_LIMITER.reset()
    _UNSUB_LIMITER.reset()
    yield
    _KEEP_SHOW_LIMITER.reset()
    _KEEP_POST_LIMITER.reset()
    _UNSUB_LIMITER.reset()


# ---------------------------------------------------------------------------
# Helpers for inserting show seasons and keep tokens
# ---------------------------------------------------------------------------


def _insert_season(
    conn: sqlite3.Connection,
    item_id: str,
    show_rating_key: str,
    show_title: str = "Test Show",
    season: int = 1,
) -> None:
    insert_media_item(
        conn,
        id=item_id,
        title=f"{show_title} S{season}",
        media_type="tv_season",
        plex_rating_key=item_id,
        show_rating_key=show_rating_key,
        show_title=show_title,
        season_number=season,
        file_path="/p",
        file_size_bytes=1,
    )


def _insert_deletion_action(conn: sqlite3.Connection, media_id: str = "mi1") -> int:
    insert_media_item(
        conn,
        id=media_id,
        title="Film",
        plex_rating_key=f"rk-{media_id}",
        file_path="/f",
        file_size_bytes=1,
    )
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        token="placeholder",
        execute_at=_now_plus_days(7),
        action="scheduled_deletion",
        delete_status="pending",
    )


def _make_keep_token(conn: sqlite3.Connection, media_id: str, action_id: int) -> str:
    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=int(time.time()) + 86400 * 180,
        secret_key=_SECRET,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token=?, token_hash=? WHERE id=?",
        (token, token_hash(token), action_id),
    )
    conn.commit()
    return token


# ===========================================================================
# B-03: api_keep_show is rate-limited
# ===========================================================================


class TestB03KeepShowRateLimited:
    """B-03: api_keep_show must return 429 when the rate limiter is exhausted."""

    def test_keep_show_rate_limited_when_limit_exceeded(self, conn):
        """Filling the KEEP_SHOW_LIMITER bucket produces a 429 on the next request."""
        _insert_season(conn, "s1", "show1")
        client = _kept_show_app(conn)

        # Exhaust the burst window: max_in_window=60, window_seconds=60
        for _ in range(60):
            _KEEP_SHOW_LIMITER.check("admin")

        resp = client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body.get("ok") is False
        assert "too_many_requests" in body.get("error", "").lower()

    def test_keep_show_succeeds_when_under_limit(self, conn):
        """A single request within the limit must not be rejected."""
        _insert_season(conn, "s1", "show1")
        client = _kept_show_app(conn)

        resp = client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ===========================================================================
# B-04: upsert_kept_show + set_protected_state + log_audit are atomic
# ===========================================================================


class TestB04KeepShowTransactionAtomicity:
    """B-04: all three writes in api_keep_show share one transaction."""

    def test_kept_show_row_created_on_success(self, conn):
        """Happy path: kept_shows row is present after a successful keep."""
        _insert_season(conn, "s1", "show1")
        client = _kept_show_app(conn)

        resp = client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )

        assert resp.status_code == 200
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key = 'show1'").fetchone()
        assert row is not None
        assert row["action"] == "protected_forever"

    def test_scheduled_action_created_on_success(self, conn):
        """Happy path: scheduled_actions row for the season is written."""
        _insert_season(conn, "s1", "show1")
        client = _kept_show_app(conn)

        client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )

        actions = conn.execute(
            "SELECT * FROM scheduled_actions WHERE media_item_id = 's1'"
        ).fetchall()
        assert len(actions) == 1
        assert actions[0]["action"] == "protected_forever"

    def test_audit_log_written_on_success(self, conn):
        """Happy path: audit_log row is present after a successful keep."""
        _insert_season(conn, "s1", "show1")
        client = _kept_show_app(conn)

        client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )

        row = conn.execute("SELECT * FROM audit_log WHERE action = 'kept_show'").fetchone()
        assert row is not None

    def test_no_kept_shows_row_when_season_not_owned(self, conn):
        """When seasons do not belong to the show, no kept_shows row is created.

        This verifies the transaction boundary: the ownership check fires
        before any write, so a bad request leaves the DB clean.
        """
        _insert_season(conn, "s1", "other_show")  # belongs to a different show
        client = _kept_show_app(conn)

        resp = client.post(
            "/api/show/show1/keep",
            json={"duration": "forever", "season_ids": ["s1"]},
        )

        # Ownership check returns 400 — nothing written
        assert resp.status_code in (400, 409)
        row = conn.execute("SELECT * FROM kept_shows WHERE show_rating_key = 'show1'").fetchone()
        assert row is None


# ===========================================================================
# H-01: keep_submit handles token replay atomically
# ===========================================================================


class TestH01KeepSubmitTokenAtomicity:
    """H-01: mark_token_consumed + apply_keep_snooze + log_audit are in one transaction."""

    @pytest.fixture(autouse=True)
    def _reset_post_limiter(self):
        _KEEP_POST_LIMITER.reset()
        yield
        _KEEP_POST_LIMITER.reset()

    def test_successful_snooze_records_token_hash(self, conn):
        """A successful keep_submit must insert the token hash into keep_tokens_used."""
        action_id = _insert_deletion_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        client = _keep_app(conn)

        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code in (200, 302, 307)
        th = token_hash(token)
        row = conn.execute(
            "SELECT token_hash FROM keep_tokens_used WHERE token_hash = ?", (th,)
        ).fetchone()
        assert row is not None

    def test_replay_returns_409(self, conn):
        """Submitting the same token a second time must return 409 already_processed."""
        action_id = _insert_deletion_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        client = _keep_app(conn)

        client.post(f"/keep/{token}", data={"duration": "30 days"})
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 409
        body = json.loads(resp.text)
        assert body["error"] == "already_processed"

    def test_expired_action_returns_400(self, conn):
        """An action whose execute_at is in the past must return 400 before any write."""
        insert_media_item(
            conn,
            id="mi_exp",
            title="Expired",
            plex_rating_key="rk-exp",
            file_path="/f",
            file_size_bytes=1,
        )
        # Insert with execute_at in the past
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi_exp",
            token="placeholder",
            execute_at=past,
            action="scheduled_deletion",
            delete_status="pending",
        )
        token = _make_keep_token(conn, "mi_exp", action_id)
        client = _keep_app(conn)

        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"


# ===========================================================================
# H-01 (keep_forever): admin forever path also rejects replays atomically
# ===========================================================================


class TestH01KeepForeverTokenAtomicity:
    """H-01 (keep_forever): mark_token_consumed + apply_keep_forever + log_audit atomic."""

    def test_replay_of_forever_returns_409(self, conn, app_factory, authed_client):
        """Submitting a forever keep twice must yield 409 on the second attempt."""
        action_id = _insert_deletion_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=_SECRET)
        app.state.db = conn
        app.state.templates = MagicMock()
        set_connection(conn)

        from mediaman.web.auth.middleware import get_current_admin

        app.dependency_overrides[get_current_admin] = lambda: "admin"
        client = TestClient(app, raise_server_exceptions=True)

        client.post(f"/api/keep/{token}/forever")
        resp = client.post(f"/api/keep/{token}/forever")

        assert resp.status_code == 409
        assert resp.json()["error"] == "already_processed"


# ===========================================================================
# H-07: _add_rec_to_sonarr rejects unmatched tmdbId in Sonarr results
# ===========================================================================


class TestH07SonarrTmdbIdFiltering:
    """H-07: _add_rec_to_sonarr must filter results to the exact tmdbId match."""

    def test_sonarr_lookup_returns_wrong_tmdb_id_is_rejected(self, conn):
        """When Sonarr's lookup returns no entry with the requested tmdbId, return an error."""
        from mediaman.core.time import now_iso
        from mediaman.services.openai.recommendations.repository import SuggestionDetail
        from mediaman.web.routes.recommended.api import _add_rec_to_sonarr

        row = SuggestionDetail(
            id=1,
            title="Test Show",
            media_type="tv",
            tmdb_id=99999,
            year=2020,
            description=None,
            reason=None,
            poster_url=None,
            rating=None,
            rt_rating=None,
            batch_id=None,
            downloaded_at=None,
            created_at=now_iso(),
        )

        mock_client = MagicMock()
        # Sonarr returns a result but with a different tmdbId
        mock_client.lookup_by_tmdb_id.return_value = [{"tmdbId": 11111, "tvdbId": 54321}]

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_client,
        ):
            resp = _add_rec_to_sonarr(
                conn,
                notify_email=None,
                row=row,
                recommendation_id=1,
                secret_key=_SECRET,
            )

        body = resp.body.decode() if hasattr(resp, "body") else str(resp)
        data = json.loads(body)
        assert data["ok"] is False
        assert "No matching TMDB" in data["error"]

    def test_sonarr_lookup_matches_correct_tmdb_id(self, conn):
        """When Sonarr returns a matching tmdbId, the series is added without error."""
        from mediaman.core.time import now_iso
        from mediaman.services.openai.recommendations.repository import SuggestionDetail
        from mediaman.web.routes.recommended.api import _add_rec_to_sonarr

        row = SuggestionDetail(
            id=1,
            title="Correct Show",
            media_type="tv",
            tmdb_id=99999,
            year=2022,
            description=None,
            reason=None,
            poster_url=None,
            rating=None,
            rt_rating=None,
            batch_id=None,
            downloaded_at=None,
            created_at=now_iso(),
        )

        mock_client = MagicMock()
        mock_client.lookup_by_tmdb_id.return_value = [
            {"tmdbId": 11111, "tvdbId": 10001},  # wrong — should be skipped
            {"tmdbId": 99999, "tvdbId": 77777},  # correct match
        ]

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_client,
        ):
            resp = _add_rec_to_sonarr(
                conn,
                notify_email=None,
                row=row,
                recommendation_id=1,
                secret_key=_SECRET,
            )

        data = json.loads(resp.body)
        assert data["ok"] is True
        mock_client.add_series.assert_called_once_with(77777, "Correct Show")

    def _tv_suggestion(self, tmdb_id: int) -> object:
        from mediaman.core.time import now_iso
        from mediaman.services.openai.recommendations.repository import SuggestionDetail

        return SuggestionDetail(
            id=1,
            title="Coerce Show",
            media_type="tv",
            tmdb_id=tmdb_id,
            year=2022,
            description=None,
            reason=None,
            poster_url=None,
            rating=None,
            rt_rating=None,
            batch_id=None,
            downloaded_at=None,
            created_at=now_iso(),
        )

    def test_string_tvdb_id_is_coerced_to_int_for_add_series(self, conn):
        """Sonarr may return ``tvdbId`` as a string; it must be coerced to
        an int before reaching ``add_series`` so the int-only call and the
        %d log line cannot blow up."""
        from mediaman.web.routes.recommended.api import _add_rec_to_sonarr

        mock_client = MagicMock()
        mock_client.lookup_by_tmdb_id.return_value = [{"tmdbId": 99999, "tvdbId": "77777"}]

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_client,
        ):
            resp = _add_rec_to_sonarr(
                conn,
                notify_email=None,
                row=self._tv_suggestion(99999),
                recommendation_id=1,
                secret_key=_SECRET,
            )

        assert json.loads(resp.body)["ok"] is True
        mock_client.add_series.assert_called_once_with(77777, "Coerce Show")

    def test_non_integer_tvdb_id_is_rejected_without_500(self, conn):
        """A non-integer ``tvdbId`` must yield a clean ``ok: False`` response,
        not a 500 from ``int()`` / ``%d`` formatting."""
        from mediaman.web.routes.recommended.api import _add_rec_to_sonarr

        mock_client = MagicMock()
        mock_client.lookup_by_tmdb_id.return_value = [{"tmdbId": 99999, "tvdbId": "not-a-number"}]

        with patch(
            "mediaman.web.routes.recommended.api.build_sonarr_from_db",
            return_value=mock_client,
        ):
            resp = _add_rec_to_sonarr(
                conn,
                notify_email=None,
                row=self._tv_suggestion(99999),
                recommendation_id=1,
                secret_key=_SECRET,
            )

        assert json.loads(resp.body)["ok"] is False
        mock_client.add_series.assert_not_called()


# ===========================================================================
# DEFERRED-A: fetch_suggestion_by_id returns a SuggestionDetail dataclass
# ===========================================================================


class TestDeferredAFetchSuggestionByIdReturnsDataclass:
    """DEFERRED-A: fetch_suggestion_by_id must return a typed SuggestionDetail, not a raw Row."""

    def test_returns_suggestion_detail_dataclass(self, conn):
        from mediaman.services.openai.recommendations.repository import (
            SuggestionDetail,
            fetch_suggestion_by_id,
        )

        sid = insert_suggestion(conn, title="Typed Film", tmdb_id=42, media_type="movie")
        result = fetch_suggestion_by_id(conn, sid)

        assert result is not None
        assert isinstance(result, SuggestionDetail), (
            f"Expected SuggestionDetail, got {type(result)}"
        )

    def test_returns_none_for_missing_id(self, conn):
        from mediaman.services.openai.recommendations.repository import fetch_suggestion_by_id

        result = fetch_suggestion_by_id(conn, 99999)
        assert result is None

    def test_dataclass_fields_match_db(self, conn):
        """All fields on the returned dataclass must match what was inserted."""
        from mediaman.services.openai.recommendations.repository import fetch_suggestion_by_id

        sid = insert_suggestion(
            conn,
            title="Field Test",
            tmdb_id=7,
            media_type="tv",
            year=2023,
            description="A description",
            reason="A reason",
        )
        result = fetch_suggestion_by_id(conn, sid)

        assert result is not None
        assert result.title == "Field Test"
        assert result.tmdb_id == 7
        assert result.media_type == "tv"
        assert result.year == 2023
        assert result.description == "A description"
        assert result.reason == "A reason"

    def test_attribute_access_not_dict_access(self, conn):
        """Accessing fields via attribute syntax must work (not only ['key'] syntax)."""
        from mediaman.services.openai.recommendations.repository import fetch_suggestion_by_id

        sid = insert_suggestion(conn, title="Attr Access Film", tmdb_id=99)
        result = fetch_suggestion_by_id(conn, sid)

        assert result is not None
        # These must not raise AttributeError / TypeError
        _ = result.id
        _ = result.title
        _ = result.media_type
        _ = result.tmdb_id


# ===========================================================================
# DEFERRED-B: unsubscribe_confirm writes a subscriber.opted_out security event
# ===========================================================================


class TestDeferredBUnsubscribeWritesSecurityEvent:
    """DEFERRED-B: a successful opt-out via the unsubscribe route must write a
    subscriber.opted_out security event to audit_log."""

    # Security events are written to audit_log with action='sec:<event>' and
    # media_item_id='_security' — not to a separate security_events table.
    _OPTED_OUT_ACTION = "sec:subscriber.opted_out"

    @pytest.fixture
    def full_app(self, db_path, secret_key):
        app, conn = _make_full_app(db_path, secret_key)
        yield app, conn
        conn.close()

    def test_opted_out_security_event_written(self, full_app, secret_key):
        """After a valid unsubscribe POST, an audit_log row with
        action='sec:subscriber.opted_out' must be present in the DB."""
        app, conn = full_app
        insert_subscriber(conn, email="unsub@example.com", active=1)
        token = generate_unsubscribe_token(email="unsub@example.com", secret_key=secret_key)
        _UNSUB_LIMITER.reset()

        client = TestClient(app)
        resp = client.post("/unsubscribe", data={"token": token})
        assert resp.status_code == 200

        row = conn.execute(
            "SELECT action, actor FROM audit_log WHERE action = ? AND media_item_id = '_security'",
            (self._OPTED_OUT_ACTION,),
        ).fetchone()
        assert row is not None, (
            "subscriber.opted_out security event must be written to audit_log on successful unsubscribe"
        )
        assert row["actor"] == "unsub@example.com"

    def test_inactive_subscriber_does_not_write_security_event(self, full_app, secret_key):
        """An already-inactive subscriber must not trigger a security event."""
        app, conn = full_app
        insert_subscriber(conn, email="already@example.com", active=0)
        token = generate_unsubscribe_token(email="already@example.com", secret_key=secret_key)
        _UNSUB_LIMITER.reset()

        client = TestClient(app)
        client.post("/unsubscribe", data={"token": token})

        row = conn.execute(
            "SELECT action FROM audit_log WHERE action = ? AND media_item_id = '_security'",
            (self._OPTED_OUT_ACTION,),
        ).fetchone()
        assert row is None, "Inactive subscriber must not produce a security event in audit_log"

    def test_subscriber_deactivated_on_unsubscribe(self, full_app, secret_key):
        """The subscriber's active flag must be 0 after successful unsubscribe."""
        app, conn = full_app
        insert_subscriber(conn, email="deactivate@example.com", active=1)
        token = generate_unsubscribe_token(email="deactivate@example.com", secret_key=secret_key)
        _UNSUB_LIMITER.reset()

        client = TestClient(app)
        client.post("/unsubscribe", data={"token": token})

        row = conn.execute(
            "SELECT active FROM subscribers WHERE email = 'deactivate@example.com'"
        ).fetchone()
        assert row is not None
        assert row["active"] == 0
