"""Unit tests for keep route — lookup_verified_action and keep_submit snooze paths."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.config import Config
from mediaman.crypto import generate_keep_token, validate_poster_token
from mediaman.db import set_connection
from mediaman.scanner.repository.scheduled_actions import (
    claim_due_deletion,
    record_removed_files,
)
from mediaman.services.scheduled_actions import (
    VerifiedKeepAction,
    lookup_verified_action,
    token_hash,
)
from mediaman.web.routes import keep as keep_module
from mediaman.web.routes.keep import (
    _KEEP_GET_LIMITER,
    _KEEP_POST_LIMITER,
)
from mediaman.web.routes.keep import router as keep_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action

# Keep tokens in this file are minted with an isolated SECRET (not the
# conftest's ``secret_key``) so the test seam stays orthogonal to the rest
# of the suite — that means the local class ``_make_app`` methods cannot
# adopt the shared ``app_factory`` (which always uses the conftest secret).
SECRET = "a" * 64
_TEMPLATE_DIR = Path(mediaman.web.__file__).parent / "templates"


def _insert_media_item(conn: sqlite3.Connection, media_id: str = "mi1") -> None:
    insert_media_item(
        conn,
        id=media_id,
        title="Test",
        plex_rating_key="rk1",
        file_path="/f",
        file_size_bytes=0,
    )


def _insert_action(
    conn: sqlite3.Connection, media_id: str = "mi1", placeholder: str = "placeholder"
) -> int:
    execute_at = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        token=placeholder,
        execute_at=execute_at,
    )


def _make_keep_token(conn: sqlite3.Connection, media_id: str, action_id: int) -> str:
    import time

    token = generate_keep_token(
        media_item_id=media_id,
        action_id=action_id,
        expires_at=int(time.time()) + 86400 * 180,
        secret_key=SECRET,
    )
    conn.execute(
        "UPDATE scheduled_actions SET token=?, token_hash=? WHERE id=?",
        (token, token_hash(token), action_id),
    )
    conn.commit()
    return token


def _pictures(html: str) -> list[str]:
    """The addresses of the hero's wide picture, its fill and its poster."""
    return re.findall(
        r'<img class="watch-banner__[^"]*" src="([^"]+)"[^>]*referrerpolicy="no-referrer"'
        r"[^>]*data-broken-on-error>",
        html,
    )


def _shown(html: str) -> str:
    """The page with its spaces run together, and no tag around its days."""
    return " ".join(re.sub(r"</?time\b[^>]*>", "", html).split())


class TestLookupVerifiedAction:
    def test_valid_token_returns_row(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, token, SECRET)

        assert row is not None
        assert row.media_item_id == "mi1"

    def test_invalid_hmac_returns_none(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, "tampered_token", SECRET)

        assert row is None

    def test_missing_db_row_returns_none(self, conn):
        """Token validates but no matching scheduled_actions row exists."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        # Delete the action so DB lookup fails
        conn.execute("DELETE FROM scheduled_actions WHERE id=?", (action_id,))
        conn.commit()

        row = lookup_verified_action(conn, token, SECRET)

        assert row is None

    def test_payload_mismatch_rejected(self, conn):
        """Token with wrong action_id in payload is rejected even if HMAC is valid."""
        import time

        _insert_media_item(conn)
        _insert_media_item(conn, "mi2")
        action_id_1 = _insert_action(conn, "mi1", "placeholder1")
        action_id_2 = _insert_action(conn, "mi2", "placeholder2")
        # Build a token for action_id_1 but point it at action_id_2's row
        token_for_1 = generate_keep_token(
            media_item_id="mi1",
            action_id=action_id_1,
            expires_at=int(time.time()) + 86400,
            secret_key=SECRET,
        )
        # Store action_id_2's token in action_id_1's DB row — payload and row disagree
        token_for_2 = generate_keep_token(
            media_item_id="mi2",
            action_id=action_id_2,
            expires_at=int(time.time()) + 86400,
            secret_key=SECRET,
        )
        conn.execute("UPDATE scheduled_actions SET token=? WHERE id=?", (token_for_2, action_id_1))
        conn.commit()

        # Looking up token_for_1 finds the row (stored token differs) → None
        row = lookup_verified_action(conn, token_for_1, SECRET)

        assert row is None

    def test_wrong_secret_returns_none(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, token, "b" * 64)

        assert row is None

    def test_returns_verified_keep_action_with_all_join_fields(self, conn):
        """lookup_verified_action returns a VerifiedKeepAction carrying both the
        scheduled_actions columns and the joined media_items display columns."""
        insert_media_item(
            conn,
            id="mi1",
            title="Some Show",
            media_type="tv",
            show_title="Some Show",
            season_number=2,
            plex_rating_key="rk-xyz",
            poster_path="/posters/abc.jpg",
            file_path="/f",
            file_size_bytes=1234,
        )
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, token, SECRET)

        assert isinstance(row, VerifiedKeepAction)
        # scheduled_actions side
        assert row.id == action_id
        assert row.media_item_id == "mi1"
        assert row.action == "scheduled_deletion"
        assert row.execute_at is not None
        assert row.token == token
        assert row.token_used == 0
        assert row.delete_status == "pending"
        assert row.token_hash == token_hash(token)
        # media_items side (the JOIN columns the route/template rely on)
        assert row.title == "Some Show"
        assert row.media_type == "tv"
        assert row.show_title == "Some Show"
        assert row.season_number == 2
        assert row.poster_path == "/posters/abc.jpg"
        assert row.file_size_bytes == 1234
        assert row.plex_rating_key == "rk-xyz"
        assert row.added_at  # NOT NULL in schema — always populated


class TestKeepLimiters:
    """GET and POST must have independent rate-limit counters."""

    def test_get_and_post_limiters_are_separate_objects(self):
        assert _KEEP_GET_LIMITER is not _KEEP_POST_LIMITER

    def test_exhausting_get_limiter_does_not_block_post_limiter(self):
        # Drain the GET limiter to its cap.
        test_ip = "10.99.0.1"
        while _KEEP_GET_LIMITER.check(test_ip):
            pass
        # GET is now exhausted for this IP.
        assert _KEEP_GET_LIMITER.check(test_ip) is False
        # POST limiter must still accept the same IP — counters are independent.
        assert _KEEP_POST_LIMITER.check(test_ip) is True


class TestKeepPageRoute:
    def _make_app(self, conn: sqlite3.Connection) -> FastAPI:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        mock_templates = MagicMock()
        mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
            json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
        )
        app.state.templates = mock_templates
        return app

    def test_overlong_token_returns_invalid(self, conn):
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/keep/{'x' * 5000}")

        assert resp.status_code == 200
        assert json.loads(resp.text)["state"] == "invalid"

    def test_invalid_token_returns_invalid_state(self, conn):
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get("/keep/badtoken")

        assert resp.status_code == 200
        assert json.loads(resp.text)["state"] == "invalid"

    def test_valid_token_returns_active_state(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/keep/{token}")

        assert resp.status_code == 200
        assert "active" in resp.text

    def test_logged_out_pictures_are_signed_and_the_poster_stands_in_for_the_wide_one(self, conn):
        _insert_media_item(conn)
        token = _make_keep_token(conn, "mi1", _insert_action(conn))
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)

        html = client.get(f"/keep/{token}").text

        wide, fill, poster = _pictures(html)
        assert wide == f"{poster}&amp;kind=art" and fill == poster
        path, _, query = poster.partition("?sig=")
        assert path == "/api/poster/rk1"
        payload = validate_poster_token(query, SECRET)
        assert payload is not None and payload["rk"] == "rk1"

    def test_the_picture_keeps_its_address_for_the_day_and_lives_no_longer(self, conn, freezer):
        freezer.move_to("2026-11-25T00:00:00+00:00")
        _insert_media_item(conn)
        token = _make_keep_token(conn, "mi1", _insert_action(conn))
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app)

        def picture(at: str) -> str:
            freezer.move_to(at)
            return _pictures(client.get(f"/keep/{token}").text)[-1]

        first = picture("2026-11-25T00:00:01+00:00")
        assert picture("2026-11-25T23:59:59+00:00") == first
        payload = validate_poster_token(first.split("?sig=")[1], SECRET)
        left = payload["exp"] - datetime.now(UTC).timestamp()
        assert payload["rk"] == "rk1" and 179 * 86400 <= left <= 180 * 86400
        assert picture("2026-11-26T00:00:01+00:00") != first

    def test_an_expired_link_still_shows_the_pictures_without_colour(self, conn, freezer):
        freezer.move_to("2026-05-10T12:00:00+00:00")
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            action="snoozed",
            execute_at="2026-05-01T12:00:00+00:00",
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)

        html = client.get(f"/keep/{token}").text

        assert len(_pictures(html)) == 3
        assert "share-page is-only is-over" in html

    @pytest.mark.parametrize(
        ("now", "deadline", "date_text", "word"),
        [
            ("2026-05-01T09:00:00+00:00", "2026-05-01T12:00:00+00:00", "1 May", "today"),
            ("2026-05-01T23:00:00+00:00", "2026-05-02T02:00:00+00:00", "2 May", "tomorrow"),
            ("2026-05-01T12:00:00+00:00", "2026-05-02T14:00:00+00:00", "2 May", "tomorrow"),
            ("2026-05-01T12:00:00+00:00", "2026-05-04T02:00:00+00:00", "4 May", "in 3 days"),
        ],
        ids=["same_day", "just_after_midnight", "one_day_two_hours", "three_days"],
    )
    def test_the_word_agrees_with_the_date_shown(
        self, conn, freezer, now, deadline, date_text, word
    ):
        freezer.move_to(now)
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=deadline
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)

        page = client.get(f"/keep/{token}").text
        text = _shown(page)

        assert f"Leaving on {date_text}</strong><span" in text
        assert re.search(rf'<span data-keep-in="2026-05-0\dT[^"]+">{word}</span>', text)
        # What the page's script needs to say the day as it falls where the reader is.
        assert re.search(r'<time data-keep-day datetime="2026-05-0\dT[^"]+\+00:00">', page)
        assert "/static/js/keep-days.js" in page

    @pytest.mark.parametrize(
        ("execute_at", "date"),
        [("2026-05-01T12:00:00+00:00", "1 May"), ("2026-05-03T11:00:00+00:00", "3 May")],
        ids=["days ago", "earlier the same day"],
    )
    def test_a_date_that_has_passed_says_so_and_that_it_can_still_be_kept(
        self, conn, freezer, execute_at, date
    ):
        freezer.move_to("2026-05-03T12:00:00+00:00")
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

        text = _shown(TestClient(app).get(f"/keep/{token}").text)

        day, month = date.split()
        assert f"<small>{month}</small><b>{day}</b>" in text
        assert "Leaving any day" in text
        assert "The date has passed, but it can still be kept." in text
        assert text.count('name="duration"') == 3

    @pytest.mark.parametrize(
        ("action", "execute_at", "duration", "text"),
        [
            (
                "snoozed",
                "2099-01-01T00:00:00+00:00",
                "30 days",
                "Already kept until 1 January 2099",
            ),
            ("protected_forever", None, "forever", "Kept for good"),
        ],
    )
    def test_row_kept_elsewhere_shows_the_kept_state(
        self, conn, action, execute_at, duration, text
    ):
        """A Library keep resets token_used to 0, so the page must read the action."""
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            action=action,
            execute_at=execute_at,
            token_used=0,
        )
        conn.execute(
            "UPDATE scheduled_actions SET snooze_duration=? WHERE id=?", (duration, action_id)
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)

        html = _shown(client.get(f"/keep/{token}").text)

        assert text in html
        assert "Leaving" not in html
        assert 'name="duration"' not in html

    def _kept_page(self, conn, freezer, follow_up: str):
        freezer.move_to("2026-11-23T12:00:00+00:00")
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"}, follow_redirects=False)
        assert resp.status_code == 302
        url = resp.headers["location"] if follow_up == "redirect" else f"/keep/{token}"
        return _shown(client.get(url).text)

    def test_page_right_after_a_keep_says_kept_not_already_kept(self, conn, freezer):
        text = self._kept_page(conn, freezer, "redirect")

        assert "Kept for 30 days" in text
        assert "Already kept" not in text

    def test_reopening_the_link_later_says_already_kept(self, conn, freezer):
        text = self._kept_page(conn, freezer, "reopen")

        assert "Already kept until 23 December" in text
        assert "Kept on 23 November, for 30 days." in text

    def test_only_the_page_right_after_a_keep_holds_still_but_for_its_date(self, conn, freezer):
        assert "share-page is-only is-just" in self._kept_page(conn, freezer, "redirect")
        conn.execute("DELETE FROM scheduled_actions")
        conn.execute("DELETE FROM media_items")
        assert "is-just" not in self._kept_page(conn, freezer, "reopen")

    @pytest.mark.parametrize("follow_up", ["redirect", "reopen"])
    def test_dates_on_the_kept_page_share_one_format(self, conn, freezer, follow_up):
        text = self._kept_page(conn, freezer, follow_up)

        assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
        assert "23 December" in text

    @pytest.mark.parametrize(
        ("leaves_in", "offered"),
        [
            (timedelta(days=5), ["7 days", "30 days", "90 days"]),
            (timedelta(days=6, hours=11), ["7 days", "30 days", "90 days"]),
            (timedelta(days=7, minutes=-1), ["30 days", "90 days"]),
            (timedelta(days=7), ["30 days", "90 days"]),
            (timedelta(days=40), ["90 days"]),
            (timedelta(days=120), []),
        ],
        ids=[
            "all",
            "the day before",
            "earlier that day",
            "the same moment",
            "only the longest",
            "none",
        ],
    )
    def test_a_length_that_ends_before_the_leaving_date_is_not_offered(
        self, conn, freezer, leaves_in, offered
    ):
        freezer.move_to("2026-11-25T12:00:00+00:00")
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            execute_at=(datetime.now(UTC) + leaves_in).isoformat(),
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app)

        html = client.get(f"/keep/{token}").text

        assert re.findall(r'name="duration" value="([^"]*)"', html) == offered
        assert ("<form" in html) is bool(offered)
        assert html.count('data-busy-label="Keeping…"') == len(offered)
        until = {"7 days": "2 Dec", "30 days": "25 Dec", "90 days": "23 Feb"}
        assert re.findall(r"until <time [^>]*>([^<]*)<", html) == [until[d] for d in offered]
        # A caller that is no page may still send it.
        sent = client.post(f"/keep/{token}", data={"duration": "7 days"}, follow_redirects=False)
        assert sent.status_code == 302

    def test_the_page_says_nothing_of_the_household(self, conn):
        insert_media_item(
            conn,
            id="mi1",
            title="Test",
            plex_rating_key="rk1",
            file_path="/films/secret/f.mkv",
            file_size_bytes=12_240_656_794,
            added_at="2019-03-02T10:00:00+00:00",
            last_watched_at="2025-05-14T10:00:00+00:00",
        )
        token = _make_keep_token(conn, "mi1", _insert_action(conn))
        app = self._make_app(conn)
        given = {}
        app.state.templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: (
            given.update(ctx)
            or Jinja2Templates(directory=str(_TEMPLATE_DIR)).TemplateResponse(req, tmpl, ctx)
        )

        html = TestClient(app).get(f"/keep/{token}").text

        assert "Test" in html and given["item"]["heading"] == "Test"
        for private in ("2019", "2025", "11.4", "12240656794", "atched", "/films/secret"):
            assert private not in html, private
            assert private not in str(given["item"]), private

    @pytest.mark.parametrize("action", ["snoozed", "scheduled_deletion"])
    def test_row_being_deleted_shows_that_state_and_no_buttons(self, conn, action):
        _insert_media_item(conn)
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            action=action,
            execute_at="2099-01-01T00:00:00+00:00",
            token_used=0,
        )
        conn.execute(
            "UPDATE scheduled_actions SET delete_status='deleting' WHERE id=?", (action_id,)
        )
        token = _make_keep_token(conn, "mi1", action_id)
        app = self._make_app(conn)
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        client = TestClient(app, raise_server_exceptions=True)

        html = _shown(client.get(f"/keep/{token}").text)

        assert "This is being deleted now, so it cannot be kept." in html
        assert "Already kept" not in html
        assert "Leaving" not in html
        assert 'name="duration"' not in html


class TestKeepSubmitTokenInvalidation:
    """Keep_submit must mark the token as used and reject replays."""

    def _make_app(self, conn: sqlite3.Connection) -> FastAPI:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        set_connection(conn)
        mock_templates = MagicMock()
        mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
            json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
        )
        app.state.templates = mock_templates
        return app

    def test_invalid_token_returns_400_invalid_or_expired(self, conn):
        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/keep/bogustoken", data={"duration": "30 days"})

        assert resp.status_code == 400
        import json as _json

        body = _json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_unknown_duration_returns_400(self, conn):
        """An unrecognised duration must return 400 before touching the DB."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post(f"/keep/{token}", data={"duration": "never"})

        assert resp.status_code == 400

    def test_token_used_flag_set_in_keep_tokens_used(self, conn):
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        # Successful POST redirects to the keep page.
        assert resp.status_code in (200, 302, 307)

        th = token_hash(token)
        row = conn.execute(
            "SELECT token_hash FROM keep_tokens_used WHERE token_hash = ?", (th,)
        ).fetchone()
        assert row is not None, "Token hash must be persisted in keep_tokens_used"

    def test_replay_returns_409_already_processed(self, conn):
        """Submitting the same token twice must return 409 on the second attempt."""
        _insert_media_item(conn)
        action_id = _insert_action(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        app = self._make_app(conn)
        client = TestClient(app, raise_server_exceptions=True)

        # First POST — should succeed.
        client.post(f"/keep/{token}", data={"duration": "30 days"})

        # Second POST — same token, must be rejected as already processed.
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 409
        import json as _json

        body = _json.loads(resp.text)
        assert body["error"] == "already_processed"


def _insert_scheduled_action_at_offset(
    conn: sqlite3.Connection,
    media_id: str = "mi1",
    action: str = "scheduled_deletion",
    delete_status: str = "pending",
    execute_at_offset_days: int = 7,
) -> int:
    """Insert a scheduled_actions row and return the rowid."""
    execute_at = (datetime.now(UTC) + timedelta(days=execute_at_offset_days)).isoformat()
    return insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action=action,
        token="placeholder",
        execute_at=execute_at,
        delete_status=delete_status,
    )


def _make_keep_app_with_test_secret(app_factory, conn: sqlite3.Connection):
    """Stand up a minimal keep-router app using the module-level test secret."""
    mock_templates = MagicMock()
    mock_templates.TemplateResponse.side_effect = lambda req, tmpl, ctx: HTMLResponse(
        json.dumps({k: str(v) for k, v in ctx.items() if k != "item"}), 200
    )
    app = FastAPI()
    app.include_router(keep_router)
    app.state.config = Config(secret_key=SECRET)
    app.state.db = conn
    app.state.templates = mock_templates
    from mediaman.db import set_connection

    set_connection(conn)
    return app, TestClient(app, raise_server_exceptions=True)


class TestForeverEndpointSeparation:
    """Forever must be refused on the public keep POST."""

    def test_forever_duration_rejected_on_public_post(self, app_factory, conn):
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "forever"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_admin_forever_endpoint_exists(self, conn):
        """POST /api/keep/{token}/forever route must be registered."""
        from mediaman.web.routes.keep import router

        routes = [r.path for r in router.routes]
        assert any("forever" in r for r in routes), (
            f"No 'forever' route found in keep router routes: {routes}"
        )

    def test_unauthenticated_forever_returns_401(self, app_factory, conn):
        """Un-authed POST to /api/keep/{token}/forever must return 401."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/api/keep/{token}/forever")
        assert resp.status_code == 401


class TestAdminForeverAudit:
    def test_admin_forever_is_audited_as_kept(self, app_factory, authed_client, conn):
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)
        app, _ = _make_keep_app_with_test_secret(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post(f"/api/keep/{token}/forever")

        assert resp.status_code == 200
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
        assert actions == ["kept"]


class TestKeepDeadlineCheck:
    """Keep_submit must reject rows that are not pending."""

    def test_a_deletion_past_its_date_can_be_kept_until_the_pass_claims_it(self, app_factory, conn):
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn, execute_at_offset_days=-1)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"}, follow_redirects=False)

        assert resp.status_code == 302
        row = conn.execute("SELECT action, execute_at FROM scheduled_actions").fetchone()
        assert row["action"] == "snoozed"
        assert row["execute_at"] > datetime.now(UTC).isoformat()

    def test_non_pending_delete_status_returns_400(self, app_factory, conn):
        """A row with delete_status='deleting' must be refused."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn, delete_status="deleting")
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_expired_kept_row_returns_400(self, app_factory, conn):
        """A keep POST against a row that is not a scheduled deletion and is past its date fails."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(
            conn, action="snoozed", execute_at_offset_days=-1
        )
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 400
        body = json.loads(resp.text)
        assert body["error"] == "invalid_or_expired"

    def test_valid_pending_action_succeeds(self, app_factory, conn):
        """A valid keep POST against a pending scheduled_deletion must succeed."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        _, client = _make_keep_app_with_test_secret(app_factory, conn)
        resp = client.post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code in (200, 302, 307)


class TestKeepTokenHash:
    """Token hash helpers and insert-only-hash logic."""

    def test_lookup_verified_action_returns_row_when_token_matches(self, conn):
        """lookup_verified_action must return the row when HMAC is valid and conditions are met."""
        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, token, SECRET)
        assert row is not None
        assert row.id == action_id

    def test_lookup_verified_action_returns_verified_keep_action_with_join_fields(self, conn):
        """lookup_verified_action returns a VerifiedKeepAction whose media_items display
        columns are populated via the JOIN."""
        insert_media_item(
            conn,
            id="mi1",
            title="Joined Title",
            media_type="movie",
            plex_rating_key="rk-join",
            poster_path="/p/join.jpg",
            file_path="/f",
            file_size_bytes=999,
        )
        action_id = _insert_scheduled_action_at_offset(conn)
        token = _make_keep_token(conn, "mi1", action_id)

        row = lookup_verified_action(conn, token, SECRET)

        assert isinstance(row, VerifiedKeepAction)
        assert row.id == action_id
        assert row.media_item_id == "mi1"
        assert row.token_hash == token_hash(token)
        # media_items columns attached by the JOIN
        assert row.title == "Joined Title"
        assert row.media_type == "movie"
        assert row.poster_path == "/p/join.jpg"
        assert row.file_size_bytes == 999
        assert row.plex_rating_key == "rk-join"

    def test_lookup_verified_action_returns_none_for_wrong_secret(self, conn):
        import time as _time

        from mediaman.crypto import generate_keep_token

        _insert_media_item(conn)
        action_id = _insert_scheduled_action_at_offset(conn)
        # Mint a token with a different secret — HMAC check must reject it
        wrong_token = generate_keep_token(
            media_item_id="mi1",
            action_id=action_id,
            expires_at=int(_time.time()) + 86400,
            secret_key="b" * 64,
        )
        row = lookup_verified_action(conn, wrong_token, SECRET)
        assert row is None

    def test_schedule_deletion_writestoken_hash(self, conn):
        from mediaman.scanner.phases.upsert import schedule_deletion

        _insert_media_item(conn)
        schedule_deletion(
            conn,
            media_id="mi1",
            is_reentry=False,
            grace_days=7,
            secret_key=SECRET,
        )
        conn.commit()

        row = conn.execute(
            "SELECT token_hash, token FROM scheduled_actions WHERE media_item_id = 'mi1'"
        ).fetchone()
        assert row is not None
        assert row["token_hash"] is not None
        assert len(row["token_hash"]) == 64


class TestKeepFormFailurePage:
    """A browser form post that fails shows a readable page, not the JSON envelope."""

    _HTML = {"accept": "text/html,application/xhtml+xml"}

    def _client(self, conn: sqlite3.Connection) -> TestClient:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        set_connection(conn)
        return TestClient(app, raise_server_exceptions=True)

    def _used_token(self, conn: sqlite3.Connection) -> str:
        _insert_media_item(conn)
        token = _make_keep_token(conn, "mi1", _insert_action(conn))
        assert self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}
        ).status_code in (
            200,
            302,
        )
        return token

    def _unprotect(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE scheduled_actions SET action='scheduled_deletion', token_used=0, "
            "snooze_duration=NULL"
        )
        conn.commit()

    def test_reused_token_renders_a_sentence_and_no_json(self, conn):
        token = self._used_token(conn)
        self._unprotect(conn)

        resp = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("text/html")
        assert "This link has been used already" in resp.text
        assert "already_processed" not in resp.text
        assert '"ok"' not in resp.text
        assert 'href="/login"' in resp.text

    def test_bad_token_renders_a_sentence_at_400(self, conn):
        resp = self._client(conn).post(
            "/keep/bogustoken", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 400
        assert "This link does not work" in resp.text
        assert "invalid_or_expired" not in resp.text

    def test_json_caller_still_gets_the_envelope(self, conn):
        token = self._used_token(conn)

        resp = self._client(conn).post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 409
        assert resp.json() == {"ok": False, "error": "already_processed"}

    def test_repeat_keep_of_a_protected_item_shows_the_protected_page(self, conn):
        token = self._used_token(conn)

        resp = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 409
        assert "Already kept until" in resp.text
        assert "used already" not in resp.text

    def test_used_token_on_an_unprotected_item_says_it_could_not_keep(self, conn):
        token = self._used_token(conn)
        self._unprotect(conn)

        resp = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 409
        assert "Nothing more was changed." in resp.text
        assert 'href="/login">Sign in</a>' in resp.text

    def test_a_dead_link_offers_the_same_sign_in_as_the_other_states(self, conn):
        resp = self._client(conn).get("/keep/badtoken")

        assert 'href="/login">Sign in</a>' in resp.text
        assert "Back to Watch" not in resp.text

    @pytest.mark.parametrize("role", ["admin", "viewer"])
    def test_someone_signed_in_is_never_told_to_sign_in(self, conn, authed_client, role, freezer):
        token = self._used_token(conn)
        client = authed_client(self._client(conn).app, conn)
        conn.execute("UPDATE admin_users SET role=? WHERE username='admin'", (role,))
        conn.commit()

        pages = [
            client.get(f"/keep/{token}"),
            client.get("/keep/badtoken"),
            client.get("/keep/" + "x" * 5000),
            client.post("/keep/badtoken", data={"duration": "30 days"}, headers=self._HTML),
        ]
        for _ in range(30):
            limited = client.get("/keep/badtoken")
        _KEEP_GET_LIMITER.reset()

        assert limited.status_code == 429
        for page in [*pages, limited]:
            assert "Sign in" not in page.text and "Open mediaman" in page.text

    @pytest.mark.parametrize(
        ("fields", "ref"),
        [
            ({"media_type": "movie", "plex_rating_key": "501"}, "movie-501"),
            (
                {"media_type": "tv_season", "plex_rating_key": "777", "show_rating_key": "88"},
                "show-88",
            ),
            ({"media_type": "movie", "plex_rating_key": "rk1"}, None),
        ],
        ids=["film", "season", "no usable key"],
    )
    @pytest.mark.parametrize("ended", [True, False], ids=["run out", "kept"])
    def test_signing_in_from_a_keep_page_leads_to_its_title(self, conn, fields, ref, ended):
        insert_media_item(conn, id="mi1", title="Test", file_path="/f", **fields)
        execute_at = datetime.now(UTC) + timedelta(days=-1 if ended else 5)
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            action="snoozed",
            execute_at=execute_at.isoformat(),
        )
        html = self._client(conn).get(f"/keep/{_make_keep_token(conn, 'mi1', action_id)}").text

        href = f"/#title={ref}" if ref else "/login"
        assert re.findall(r'href="([^"]*)">Sign in', html) == [href, href]

    def test_an_unknown_error_code_still_renders_a_page(self, conn):
        from starlette.requests import Request

        from mediaman.web.routes.keep import _submit_failure

        client = self._client(conn)
        scope = {
            "type": "http",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"accept", b"text/html"),
            ],
            "app": client.app,
            "method": "POST",
            "path": "/keep/x",
            "query_string": b"",
        }

        resp = _submit_failure(Request(scope), "something_new", 500)

        assert resp.status_code == 500
        assert b"nothing was changed" in resp.body


class TestItemKeptMeanwhile:
    """An unused token on an item kept another way answers as a repeat keep does."""

    _HTML = {"accept": "text/html,application/xhtml+xml"}

    def _client(self, conn: sqlite3.Connection) -> TestClient:
        app = FastAPI()
        app.include_router(keep_router)
        app.state.config = Config(secret_key=SECRET)
        app.state.db = conn
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
        set_connection(conn)
        return TestClient(app, raise_server_exceptions=True)

    def _kept_by_admin(self, conn: sqlite3.Connection) -> str:
        _insert_media_item(conn)
        token = _make_keep_token(conn, "mi1", _insert_action(conn))
        conn.execute(
            "UPDATE scheduled_actions SET action='snoozed', snooze_duration='30 days', "
            "snoozed_at=? WHERE media_item_id='mi1'",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
        return token

    @staticmethod
    def _action_row(conn: sqlite3.Connection) -> tuple:
        return tuple(
            conn.execute(
                "SELECT action, execute_at, token_used, snoozed_at, snooze_duration "
                "FROM scheduled_actions WHERE media_item_id='mi1'"
            ).fetchone()
        )

    def test_form_post_shows_the_protected_page(self, conn):
        token = self._kept_by_admin(conn)
        before = self._action_row(conn)

        resp = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 409
        assert "Already kept until" in resp.text
        assert "This link does not work" not in resp.text
        assert self._action_row(conn) == before

    def test_json_post_answers_like_a_repeat_keep(self, conn):
        token = self._kept_by_admin(conn)
        before = self._action_row(conn)

        resp = self._client(conn).post(f"/keep/{token}", data={"duration": "30 days"})

        assert resp.status_code == 409
        assert resp.json() == {"ok": False, "error": "already_processed"}
        assert self._action_row(conn) == before

    def test_admin_forever_answers_like_a_repeat_keep(self, app_factory, authed_client, conn):
        token = self._kept_by_admin(conn)
        app = self._client(conn).app
        client = authed_client(app, conn)
        before = self._action_row(conn)

        resp = client.post(f"/api/keep/{token}/forever")

        assert resp.status_code == 409
        assert resp.json()["error"] == "already_processed"
        assert self._action_row(conn) == before

    def test_a_lapsed_keep_is_still_refused(self, conn):
        _insert_media_item(conn)
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn,
            media_item_id="mi1",
            token="placeholder",
            action="snoozed",
            execute_at=execute_at,
        )
        token = _make_keep_token(conn, "mi1", action_id)

        form = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )
        api = self._client(conn).post(f"/keep/{token}", data={"duration": "30 days"})

        assert form.status_code == 400
        assert "This link does not work" in form.text
        assert api.status_code == 400
        assert api.json()["error"] == "invalid_or_expired"

    def test_a_keep_after_the_deletion_began_says_it_is_being_deleted(self, conn):
        _insert_media_item(conn)
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        assert claim_due_deletion(conn, action_id, datetime.now(UTC).isoformat())
        conn.commit()
        before = self._action_row(conn)

        resp = self._client(conn).post(
            f"/keep/{token}", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 400
        assert "This is being deleted now," in resp.text
        assert self._action_row(conn) == before

    @pytest.mark.parametrize("forever", [False, True])
    def test_a_keep_that_loses_to_the_deletion_leaves_its_link_for_when_that_fails(
        self, app_factory, authed_client, conn, monkeypatch, forever
    ):
        _insert_media_item(conn)
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        client = authed_client(self._client(conn).app, conn)

        def claimed_meanwhile(route_conn, token_hash):
            assert claim_due_deletion(route_conn, action_id, datetime.now(UTC).isoformat())
            route_conn.commit()
            return False

        def keep():
            if forever:
                return client.post(f"/api/keep/{token}/forever").status_code
            return client.post(
                f"/keep/{token}", data={"duration": "30 days"}, follow_redirects=False
            ).status_code

        with monkeypatch.context() as patched:
            patched.setattr(keep_module, "is_keep_token_consumed", claimed_meanwhile)
            assert keep() == 409
        conn.execute("UPDATE scheduled_actions SET delete_status = 'pending'")
        conn.commit()

        assert keep() == (200 if forever else 302)

    @pytest.mark.parametrize(("left", "status"), [([], 400), (["/m/b.mkv"], 302)])
    def test_only_a_deletion_that_left_files_can_be_kept(self, conn, left, status):
        _insert_media_item(conn)
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        conn.execute(
            "UPDATE media_items SET tracked_files = ?",
            (json.dumps([{"path": f, "size": 1} for f in ["/m/a.mkv", *left]]),),
        )
        record_removed_files(conn, action_id, {"/m/a.mkv"}, 1, {"/m/a.mkv": 1}, left)
        conn.commit()
        client = self._client(conn)

        page = client.get(f"/keep/{token}").text
        resp = client.post(
            f"/keep/{token}",
            data={"duration": "30 days"},
            headers=self._HTML,
            follow_redirects=False,
        )

        assert ('name="duration"' in page, resp.status_code) == (bool(left), status)
        action = conn.execute("SELECT action FROM scheduled_actions").fetchone()["action"]
        assert action == ("snoozed" if left else "scheduled_deletion")
        assert ("This is being deleted now," in page) is (not left)

    def test_a_forever_keep_made_past_the_date_reads_protected(
        self, app_factory, authed_client, conn
    ):
        _insert_media_item(conn)
        execute_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        action_id = insert_scheduled_action(
            conn, media_item_id="mi1", token="placeholder", execute_at=execute_at
        )
        token = _make_keep_token(conn, "mi1", action_id)
        client = authed_client(self._client(conn).app, conn)

        assert client.post(f"/api/keep/{token}/forever").status_code == 200

        html = client.get(f"/keep/{token}").text
        assert "This link has run out" not in html
        assert "Kept for good" in html

    def test_a_token_that_fails_verification_never_shows_the_protected_page(self, conn):
        token = self._kept_by_admin(conn)

        resp = self._client(conn).post(
            f"/keep/{token}x", data={"duration": "30 days"}, headers=self._HTML
        )

        assert resp.status_code == 400
        assert "Already kept" not in resp.text
        assert "This link does not work" in resp.text
