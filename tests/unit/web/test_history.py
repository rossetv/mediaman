"""Tests for the history API — paginated audit log with action-type filter."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.core.audit import log_audit, security_event
from mediaman.scanner.repository.audit import (
    AuditRow,
    fetch_media_audit_rows,
    fetch_security_audit_rows,
)
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.history import _PER_PAGE_DEFAULT, _PER_PAGE_MAX, ACTION_BADGE_CLASS
from mediaman.web.routes.history import router as history_router
from tests.helpers.factories import insert_audit_log, insert_kept_show, insert_media_item


def _insert_audit_row(conn, action: str = "scanned", media_item_id: str = "m1") -> None:
    insert_audit_log(conn, media_item_id=media_item_id, action=action)


class TestApiHistory:
    def test_history_requires_auth(self, app_factory, conn):
        app = app_factory(history_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/history")
        assert resp.status_code == 401

    def test_history_empty_returns_valid_shape(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []
        assert body["page"] == 1
        assert "per_page" in body
        assert "total_pages" in body

    def test_history_returns_rows(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        for action in ("scanned", "deleted", "kept"):
            _insert_audit_row(conn, action=action)
        resp = client.get("/api/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3
        item = body["items"][0]
        assert "action" in item
        assert "created_at" in item

    def test_history_action_filter(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        _insert_audit_row(conn, action="deleted", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        _insert_audit_row(conn, action="scanned", media_item_id="m3")
        resp = client.get("/api/history?action=deleted")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(i["action"] == "deleted" for i in body["items"])

    def test_history_invalid_action_filter_ignored(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        _insert_audit_row(conn, action="scanned", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        resp = client.get("/api/history?action=bogus_action_xyz")
        assert resp.status_code == 200
        # Invalid filter is silently dropped — all rows returned
        assert resp.json()["total"] == 2

    def test_history_pagination(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        for i in range(5):
            _insert_audit_row(conn, media_item_id=f"m{i}")

        resp = client.get("/api/history?per_page=2&page=1")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["total_pages"] == 3

        resp = client.get("/api/history?per_page=2&page=3")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_per_page_max_enforced(self, app_factory, authed_client, conn):
        """per_page above the maximum must be clamped/rejected by the Query constraint."""
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.get(f"/api/history?per_page={_PER_PAGE_MAX + 1}")
        # FastAPI Query(le=...) returns 422 Unprocessable Entity for out-of-range values.
        assert resp.status_code == 422

    def test_per_page_zero_rejected(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/history?per_page=0")
        assert resp.status_code == 422

    def test_shared_per_page_constants(self):
        """_PER_PAGE_DEFAULT and _PER_PAGE_MAX are within sensible ranges."""
        assert 1 <= _PER_PAGE_DEFAULT <= _PER_PAGE_MAX
        assert _PER_PAGE_MAX <= 100


def _insert_audit_full(
    conn,
    *,
    action: str,
    media_item_id: str,
    detail: str | None = None,
) -> None:
    """Insert an audit row with optional detail body — used by the
    title-resolution / detail-scrubbing tests."""
    insert_audit_log(conn, media_item_id=media_item_id, action=action, detail=detail)


def _insert_kept_show(conn, *, rating_key: str, title: str) -> None:
    """Insert a kept_shows row so the show-action JOIN can resolve."""
    insert_kept_show(conn, show_rating_key=rating_key, show_title=title, action="kept_show")


class TestHistoryPageClamp:
    """``GET /history`` must clamp ``?page=`` to ``total_pages`` so a stale
    or hostile URL doesn't run a wasteful OFFSET sweep AND doesn't render
    a misleading ``Page 9999 of 1`` footer."""

    def _make_page_app(self, app_factory, conn):
        from unittest.mock import MagicMock

        from fastapi.responses import HTMLResponse

        mock_templates = MagicMock()

        def fake_template_response(request, template_name, ctx):
            # Echo the rendered ``page`` and ``total_pages`` numbers so the
            # test can assert against the clamped values.
            return HTMLResponse(
                f"page={ctx['page']};total_pages={ctx['total_pages']};total={ctx['total']}",
                status_code=200,
            )

        mock_templates.TemplateResponse.side_effect = fake_template_response
        return app_factory(history_router, conn=conn, state_extras={"templates": mock_templates})

    def test_page_clamped_to_total_pages(self, app_factory, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        # Insert 3 rows — one page at per_page=25.
        for i in range(3):
            _insert_audit_row(conn, media_item_id=f"m{i}")

        app = self._make_page_app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        resp = client.get("/history?page=10000")
        assert resp.status_code == 200
        # Clamped to 1 (total_pages=1 for 3 rows).
        assert "page=1;" in resp.text
        assert "total_pages=1" in resp.text

    def test_negative_page_clamped_to_one(self, app_factory, conn):
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")

        _insert_audit_row(conn, media_item_id="m0")

        app = self._make_page_app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)

        # ``int("-5")`` is valid but max(1, -5) clamps the lower end.
        resp = client.get("/history?page=-5")
        assert "page=1;" in resp.text


class TestKeptShowJoinDoesNotLeakMovieTitle:
    """The kept_show audit JOIN must not pick up a movie title even when
    the rating-key namespaces collide (defensive: today's Plex doesn't
    surface that, but a future migration could)."""

    def test_kept_show_resolves_to_show_title(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        # Same rating-key value exists as both a movie and a show.
        insert_media_item(
            conn,
            id="42",
            title="Wrong Movie Title",
            media_type="movie",
            plex_rating_key="42",
            added_at=datetime(2025, 1, 1, tzinfo=UTC),
            file_path="/tmp/x",
            file_size_bytes=0,
        )
        _insert_kept_show(conn, rating_key="42", title="Right Show Title")
        _insert_audit_full(conn, action="kept_show", media_item_id="42")

        resp = client.get("/api/history?action=kept_show")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # The kept_show JOIN must produce the show title, not the movie title.
        assert items[0]["title"] == "Right Show Title"

    def test_movie_kept_does_not_pull_show_title(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        # Set up a movie audit row whose rating-key happens to collide
        # with a kept_show entry.
        insert_media_item(
            conn,
            id="99",
            title="Real Movie",
            media_type="movie",
            plex_rating_key="99",
            added_at=datetime(2025, 1, 1, tzinfo=UTC),
            file_path="/tmp/x",
            file_size_bytes=0,
        )
        _insert_kept_show(conn, rating_key="99", title="Wrong Show")
        _insert_audit_full(conn, action="kept", media_item_id="99")

        resp = client.get("/api/history?action=kept")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # ``kept`` is NOT a show action, so the kept_shows JOIN must miss
        # and the title comes from media_items.
        assert items[0]["title"] == "Real Movie"


class TestDetailControlByteScrubbing:
    """The ``detail`` blob is rendered into the history page UI and the
    JSON API.  A future audit row whose detail carried a CR/LF or a
    terminal escape must not corrupt either."""

    def test_control_bytes_removed_from_response(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        _insert_audit_full(
            conn,
            action="scanned",
            media_item_id="m1",
            detail="line1\x00\x1b[31mred\x07bell",
        )

        resp = client.get("/api/history")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        detail = items[0]["detail"]
        assert "\x00" not in detail
        assert "\x1b" not in detail
        assert "\x07" not in detail

    def test_visible_whitespace_preserved(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        _insert_audit_full(
            conn,
            action="scanned",
            media_item_id="m1",
            detail="line1\nline2\tcol2",
        )

        resp = client.get("/api/history")
        items = resp.json()["items"]
        # Newline and tab are visible whitespace — must NOT be stripped.
        assert "\n" in items[0]["detail"]
        assert "\t" in items[0]["detail"]


class TestAuditRowShape:
    """``fetch_media_audit_rows`` / ``fetch_security_audit_rows`` return a
    frozen, slotted ``AuditRow`` dataclass — never a raw ``sqlite3.Row``.
    Both queries project the same nine-field shape.
    """

    def test_media_audit_rows_return_audit_rows(self, conn):
        insert_audit_log(conn, media_item_id="m1", action="scanned", detail="hello")
        rows = fetch_media_audit_rows(conn, action=None, page=1, per_page=25)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, AuditRow)
        # Attribute access — not subscript.
        assert row.media_item_id == "m1"
        assert row.action == "scanned"
        assert row.detail == "hello"

    def test_security_audit_rows_return_audit_rows(self, conn):
        from mediaman.core.audit import security_event

        security_event(conn, event="settings.write", actor="admin", ip="127.0.0.1", detail="d")
        rows = fetch_security_audit_rows(conn, page=1, per_page=25)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, AuditRow)
        assert row.action == "sec:settings.write"
        # Security rows project NULL for the media-title columns.
        assert row.mi_title is None
        assert row.ks_title is None

    def test_audit_row_is_frozen_and_slotted(self):
        row = AuditRow(
            id=1,
            media_item_id="m1",
            action="scanned",
            detail=None,
            space_reclaimed_bytes=None,
            created_at="2026-01-01T00:00:00+00:00",
            mi_title=None,
            plex_rating_key=None,
            ks_title=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            row.action = "mutated"  # type: ignore[misc]
        assert not hasattr(row, "__dict__")


class TestSecurityTitleHoist:
    """For ``sec:*`` rows the title must be the event name, not a
    parsed snippet of the detail — even when the detail happens to
    contain a single-quoted phrase that the regex would otherwise grab."""

    def test_security_title_does_not_pull_quoted_string_from_detail(
        self, app_factory, authed_client, conn
    ):
        from mediaman.core.audit import security_event

        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)

        # Detail body contains a single-quoted phrase that the legacy
        # regex would have lifted into the page title.
        security_event(
            conn,
            event="settings.write",
            actor="admin",
            ip="127.0.0.1",
            detail="rotated 'plex_token' value",
        )

        resp = client.get("/api/history?action=security")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        # Must be the event label, not "plex_token".
        assert items[0]["title"] == "Settings changed"
        assert items[0]["title"] != "plex_token"


class TestDeleteFailedAction:
    def test_failed_delete_is_labelled_and_filterable(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn)
        client = authed_client(app, conn)
        _insert_audit_row(conn, action="delete_failed")
        _insert_audit_row(conn, action="scanned", media_item_id="m2")
        body = client.get("/api/history", params={"action": "delete_failed"}).json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["action_label"] == "delete failed"
        assert item["badge_class"] == "badge-action-deleted"

    def test_delete_failed_filter_includes_permanent_failures(
        self, app_factory, authed_client, conn
    ):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_row(conn, action="delete_failed", media_item_id="m1")
        _insert_audit_row(conn, action="delete_failed_permanent", media_item_id="m2")
        _insert_audit_row(conn, action="deleted", media_item_id="m3")
        body = client.get("/api/history", params={"action": "delete_failed"}).json()
        assert body["total"] == 2
        assert {i["action"] for i in body["items"]} == {"delete_failed", "delete_failed_permanent"}


class TestSchedulerAndMaintenanceActions:
    def test_scheduled_filter_returns_scheduled_deletion_rows(
        self, app_factory, authed_client, conn
    ):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_row(conn, action="scheduled_deletion", media_item_id="m1")
        _insert_audit_row(conn, action="deleted", media_item_id="m2")
        body = client.get("/api/history", params={"action": "scheduled"}).json()
        assert body["total"] == 1
        assert body["items"][0]["action"] == "scheduled_deletion"

    def test_scanned_is_not_a_filter_option(self):
        from mediaman.web.routes.history import ACTION_TYPES

        assert "scanned" not in ACTION_TYPES

    @pytest.mark.parametrize(
        ("action", "label"),
        [
            ("delete_failed_permanent", "delete failed"),
            ("delete_refused", "delete refused"),
            ("dry_run_skip", "dry run: skipped"),
            ("media_missing", "media missing"),
            ("unmonitor_skipped", "unmonitor skipped"),
            ("delete_root_unavailable", "delete root unavailable"),
            ("delete_root_available", "delete root available"),
            ("delete_awaiting_scan", "waiting"),
            ("delete_incomplete", "delete incomplete"),
            ("delete_folders_failed", "folder cleanup failed"),
            ("re_downloaded", "re-downloaded"),
        ],
    )
    def test_action_has_plain_label(self, app_factory, authed_client, conn, action, label):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_row(conn, action=action)
        item = client.get("/api/history").json()["items"][0]
        assert item["action_label"] == label

    @pytest.mark.parametrize(
        ("why", "label"),
        [
            ("it may be a title of a library Plex does not list now", "waiting for library"),
            ("its files are not recorded yet; it waits for a scan to record", "waiting for scan"),
            ("its library is not selected in Settings", "library not selected"),
            (
                "its file is of a kept title; it waits while that one is kept",
                "waiting for keep to end",
            ),
            ("it waits until that watch is too long ago", "waiting for watch to age"),
            ("of a library whose disk limit in Settings is bad", "waiting for disk limit"),
            ("its folder also holds media whose own deletion waits", "waiting for folder"),
        ],
    )
    def test_a_waiting_deletion_says_what_it_waits_for(
        self, app_factory, authed_client, conn, why, label
    ):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
            "VALUES ('m1', 'delete_awaiting_scan', ?, '2026-01-01T00:00:00+00:00')",
            (f"Not deleted yet: Film — {why}",),
        )
        conn.commit()
        item = client.get("/api/history").json()["items"][0]
        assert item["action_label"] == label

    @pytest.mark.parametrize("path", ["/api/history", "/api/security-events"])
    def test_a_page_past_any_count_is_refused(self, app_factory, authed_client, conn, path):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        assert client.get(path, params={"page": "9" * 20}).status_code == 422

    def test_delete_root_recovery_is_not_badged_as_an_alarm(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_row(conn, action="delete_root_available")
        item = client.get("/api/history").json()["items"][0]
        assert item["badge_class"] != "badge-action-deleted"
        assert item["badge_class"] in ACTION_BADGE_CLASS.values()
        assert "delete_root_available" in ACTION_BADGE_CLASS

    def test_delete_root_row_shows_folder_as_title(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_row(
            conn, action="delete_root_unavailable", media_item_id="_delete_root:/mnt/x"
        )
        item = client.get("/api/history").json()["items"][0]
        assert item["title"] == "/mnt/x"


def _summaries(html: str) -> list[str]:
    return re.findall(r"<summary.*?</summary>", html, re.S)


def _bodies(html: str) -> list[str]:
    return re.findall(r'<div class="hist-body">.*?</details>', html, re.S)


_TEMPLATE_DIR = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"


class TestHistoryPillColour:
    """The pill colour follows the action's badge class, not words in its label."""

    @pytest.mark.parametrize(
        ("action", "variant"),
        [
            ("deleted", "stale"),
            ("delete_failed_permanent", "stale"),
            ("delete_root_unavailable", "stale"),
            ("delete_folders_failed", "stale"),
            ("delete_awaiting_scan", "neutral"),
            ("delete_root_available", "neutral"),
            ("kept", "kept"),
            ("downloaded", "neutral"),
            ("scanned", "neutral"),
            ("sec:login.failed", "neutral"),
            ("sec:user.deleted", "neutral"),
            ("some_future_action", "neutral"),
        ],
    )
    def test_pill_variant_comes_from_badge_class(
        self, app_factory, authed_client, conn, action, variant
    ):
        _insert_audit_row(conn, action=action)
        app = app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        resp = authed_client(app, conn).get("/history")
        assert resp.status_code == 200
        pills = re.findall(r'<span class="pill pill--(\w+)">', resp.text)
        assert pills == [variant]


class TestMergedKeepFilters:
    def _actions(self, app_factory, authed_client, conn, action):
        if not hasattr(self, "_client"):
            self._client = authed_client(app_factory(history_router, conn=conn), conn)
        resp = self._client.get("/api/history", params={"action": action})
        return {i["action"] for i in resp.json()["items"]}

    def _seed(self, conn):
        for n, a in enumerate(["snoozed", "protected", "unkept", "removed_show_keep", "deleted"]):
            insert_audit_log(conn, media_item_id=f"m{n}", action=a)

    def test_kept_filter_covers_snoozed_and_kept(self, app_factory, authed_client, conn):
        self._seed(conn)
        got = self._actions(app_factory, authed_client, conn, "kept")
        assert got == {"snoozed", "protected"}

    def test_keep_removed_filter_covers_unkept_and_show_keep_removal(
        self, app_factory, authed_client, conn
    ):
        self._seed(conn)
        got = self._actions(app_factory, authed_client, conn, "unkept")
        assert got == {"unkept", "removed_show_keep"}

    def test_old_single_action_values_still_filter_exactly(self, app_factory, authed_client, conn):
        self._seed(conn)
        assert self._actions(app_factory, authed_client, conn, "snoozed") == {"snoozed"}
        assert self._actions(app_factory, authed_client, conn, "removed_show_keep") == {
            "removed_show_keep"
        }

    def test_pills_show_one_kept_and_one_keep_removed(self, app_factory, authed_client, conn):
        app = app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        html = authed_client(app, conn).get("/history").text
        assert html.count(">Kept<") == 1
        assert html.count(">Keep removed<") == 1
        assert "?action=snoozed" not in html
        assert "?action=removed_show_keep" not in html


class TestHistoryFilterButtons:
    def test_redownloaded_filter_button_is_hyphenated(self, app_factory, authed_client, conn):
        app = app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        resp = authed_client(app, conn).get("/history")
        assert resp.status_code == 200
        assert "Re-downloaded" in resp.text
        assert "Re Downloaded" not in resp.text
        assert "Delete failed" in resp.text


@pytest.mark.parametrize(
    ("reason", "summary"),
    [
        ("its disk limit in Settings cannot be read", "Check its disk limit in Settings"),
        ("Plex failed to answer for it", "Plex failed to answer for it"),
    ],
)
def test_a_library_left_out_is_named_with_what_to_check(
    app_factory, authed_client, conn, reason, summary
):
    from mediaman.scanner.runner import _audit_left_out

    _audit_left_out(conn, "k", {"1": reason}, {"1": "kids' films / shorts"})
    client = authed_client(app_factory(history_router, conn=conn), conn)

    [item] = client.get("/api/history").json()["items"]

    assert (item["action_label"], item["title"], item["line_title"], item["summary"]) == (
        "library not scanned",
        "kids' films / shorts",
        "kids' films / shorts",
        summary,
    )
    assert item["badge_class"] == "badge-action-deleted"


def test_only_the_line_of_a_library_left_out_may_wrap(app_factory, authed_client, conn):
    from mediaman.scanner.runner import _audit_left_out

    _audit_left_out(conn, "k", {"1": "Plex failed to answer for it"}, {"1": "Films"})
    _insert_audit_row(conn)
    app = app_factory(
        history_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )

    html = authed_client(app, conn).get("/history").text

    assert html.count('class="hist-msg"') == 1
    [wrapped] = re.findall(r'class="hist-msg hist-msg--wrap">.*?<b>([^<]*)', html)
    assert wrapped == "Films"


def test_a_long_library_name_is_cut_by_itself_and_is_whole_in_the_details(
    app_factory, authed_client, conn
):
    from mediaman.scanner.runner import _audit_left_out

    name = "Very long name " * 40
    _audit_left_out(conn, "k", {"1": "Plex failed to answer for it"}, {"1": name})
    app = app_factory(
        history_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )

    html = authed_client(app, conn).get("/history").text

    cut, hint = re.search(
        r'<span class="hist-name">(.*?)</span>\s*<span class="hist-type">(.*?)</span>', html
    ).groups()
    assert name in cut and "Plex failed" not in cut and "·" not in cut
    assert hint == "·&nbsp;Plex failed to answer for it"
    assert re.search(rf"<dt>Title</dt><dd>{name}</dd>", html)
    css = (_TEMPLATE_DIR.parent / "static/css/_history.css").read_text(encoding="utf-8")
    assert "text-overflow: ellipsis" in re.search(r"\.hist-name b \{([^}]*)\}", css)[1]
    assert "line-clamp" not in css


def test_history_delete_root_prefix_matches_the_scanner_writer():
    from mediaman.scanner.deletions import _FOLDER_AUDIT_ID
    from mediaman.web.routes.history import _DELETE_ROOT_PREFIX

    assert _DELETE_ROOT_PREFIX == _FOLDER_AUDIT_ID


class TestScannerAuditTitles:
    def test_unquoted_scanner_details_resolve_to_the_title(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        for i, detail in enumerate(
            [
                "Deleted: Dune [rk:123]",
                "Deleted (recovered): Dune [rk:123]",
                "Not deleted: Dune \u2014 disk is read-only",
            ]
        ):
            _insert_audit_full(conn, action="deleted", media_item_id=f"gone-{i}", detail=detail)

        titles = {item["title"] for item in client.get("/api/history").json()["items"]}

        assert titles == {"Dune"}

    def test_unrelated_detail_still_reads_unknown(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        _insert_audit_full(conn, action="scanned", media_item_id="x", detail="Scan finished")

        [item] = client.get("/api/history").json()["items"]

        assert item["title"] == "Unknown"


class TestScannerTitlesWithPunctuation:
    def test_apostrophes_and_em_dashes_stay_in_the_title(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(history_router, conn=conn), conn)
        details = {
            "a": "Deleted: Schindler's List: Director's Cut [rk:1]",
            "b": "Deleted: Star Wars — A New Hope [rk:2]",
            "c": "Deleted 'Schindler's List' by admin [rk:3]",
        }
        for key, detail in details.items():
            _insert_audit_full(conn, action="deleted", media_item_id=key, detail=detail)

        titles = {i["title"] for i in client.get("/api/history").json()["items"]}

        assert titles == {
            "Schindler's List: Director's Cut",
            "Star Wars — A New Hope",
            "Schindler's List",
        }


class TestHistoryBadgesAndSecurityRows:
    def _page(self, app_factory, authed_client, conn) -> str:
        app = app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        resp = authed_client(app, conn).get("/history")
        assert resp.status_code == 200
        return resp.text

    def test_keep_rows_are_badged_kept_and_removal_as_keep_removed(
        self, app_factory, authed_client, conn
    ):
        insert_audit_log(
            conn, media_item_id="m1", action="snoozed", detail="Kept for 30 days by admin (a)"
        )
        insert_audit_log(
            conn, media_item_id="m2", action="unprotected", detail="Protection removed by a"
        )
        html = self._page(app_factory, authed_client, conn)
        assert re.findall(r'<span class="pill pill--(\w+)">\s*([^<]+?)\s*</span>', html) == [
            ("neutral", "keep removed"),
            ("kept", "kept"),
        ]

    def test_security_rows_show_readable_label_and_no_raw_json(
        self, app_factory, authed_client, conn
    ):
        from mediaman.core.audit import security_event

        security_event(
            conn, event="login.success", actor="admin", ip="10.0.0.1", detail={"ua_hash": "abc123"}
        )
        security_event(conn, event="user.created", actor="admin", ip="10.0.0.1")
        html = self._page(app_factory, authed_client, conn)
        lines = " ".join(_summaries(html))
        assert "Signed in" in lines
        assert "User created" in lines
        assert "login.success" not in lines
        assert "10.0.0.1" not in lines
        assert "ua_hash" not in html
        assert "abc123" not in html
        assert "10.0.0.1" in _bodies(html)[1]
        assert "sec:login.success" in _bodies(html)[1]

    def test_security_row_with_spaced_actor_and_unlabelled_event(
        self, app_factory, authed_client, conn
    ):
        from mediaman.core.audit import security_event

        security_event(conn, event="aes.canary_failed", actor="Jane Doe", ip="10.0.0.2")
        html = self._page(app_factory, authed_client, conn)
        assert "Jane Doe" not in _summaries(html)[0]
        assert "10.0.0.2" not in _summaries(html)[0]
        assert "Jane Doe" in _bodies(html)[0]
        assert "10.0.0.2" in _bodies(html)[0]
        assert "Aes canary" not in html

    def test_filter_pills_use_row_labels(self, app_factory, authed_client, conn):
        html = self._page(app_factory, authed_client, conn)
        assert ">Keep removed<" in html
        assert "Unprotected" not in html
        assert "Removed Show Keep" not in html


class TestHistoryPageMarkup:
    def _page(self, app_factory, authed_client, conn, query: str) -> str:
        app = app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        resp = authed_client(app, conn).get(f"/history{query}")
        assert resp.status_code == 200
        return resp.text

    def test_the_pager_names_its_buttons_and_marks_the_current_page(
        self, app_factory, authed_client, conn
    ):
        for i in range(_PER_PAGE_DEFAULT * 2 + 1):
            insert_audit_log(conn, media_item_id=f"m{i}", action="deleted")
        html = self._page(app_factory, authed_client, conn, "?page=2")
        nav = re.search(
            r'<nav class="pagination hist-pager" aria-label="([^"]+)">(.*?)</nav>', html, re.S
        )
        assert nav
        buttons = nav.group(2)
        assert 'aria-label="Previous page"' in buttons
        assert 'aria-label="Next page"' in buttons
        assert re.search(r'role="link" aria-current="page"[^>]*>2<', buttons)
        assert 'aria-label="Page 1"' in buttons

    def test_a_disabled_arrow_is_a_link_role_so_its_name_and_state_are_read(
        self, app_factory, authed_client, conn
    ):
        for i in range(_PER_PAGE_DEFAULT + 1):
            insert_audit_log(conn, media_item_id=f"m{i}", action="deleted")
        html = self._page(app_factory, authed_client, conn, "")
        assert 'role="link" aria-label="Previous page" aria-disabled="true"' in html

    def test_on_a_phone_only_the_arrows_and_the_page_count_remain(
        self, app_factory, authed_client, conn
    ):
        for i in range(_PER_PAGE_DEFAULT * 9):
            insert_audit_log(conn, media_item_id=f"m{i}", action="deleted")
        html = self._page(app_factory, authed_client, conn, "?page=5")
        nav = re.search(r'<nav class="pagination hist-pager".*?</nav>', html, re.S)[0]
        classes = re.findall(r'<(?:a|span)[^>]*class="(page-btn[^"]*)"', nav)
        shown_on_phone = [c for c in classes if "page-btn--num" not in c]
        assert [("page-next" in c) for c in shown_on_phone] == [False, True]
        assert len(classes) > 6  # numbers and an ellipsis are in the page, for wider screens
        css = (_TEMPLATE_DIR.parent / "static" / "css" / "_history.css").read_text()
        assert re.search(
            r"max-width: 699\.98px\)\s*\{[^@]*\.hist-pager \.page-btn--num \{ display: none", css
        )

    def test_pills_by_touch_keep_rows_apart_by_twice_their_reach_band(self):
        css = (_TEMPLATE_DIR.parent / "static" / "css" / "_history.css").read_text()
        block = re.search(
            r"@media \(pointer: coarse\) and \(min-width: 700px\) \{(.*?)\n\}", css, re.S
        )[1]
        band = float(
            re.search(r"\.hist-filters \.fpill::after \{[^}]*inset: -([\d.]+)px", block)[1]
        )
        gap = float(re.search(r"\.pill-row\.hist-filters \{ row-gap: ([\d.]+)px", block)[1])
        assert gap >= 2 * band

    def test_the_last_row_does_not_double_the_card_border(self):
        css = (_TEMPLATE_DIR.parent / "static" / "css" / "_history.css").read_text()
        assert re.search(r"\.hist:last-child\s*\{\s*border-bottom:\s*0", css)

    def test_the_event_count_is_styled_as_a_quiet_count(self, app_factory, authed_client, conn):
        insert_audit_log(conn, media_item_id="m1", action="deleted")
        html = self._page(app_factory, authed_client, conn, "")
        assert 'class="hist-count ' in html

    def test_an_old_link_to_a_show_keep_removal_lights_the_keep_removed_pill(
        self, app_factory, authed_client, conn
    ):
        insert_audit_log(conn, media_item_id="m1", action="deleted")
        html = self._page(app_factory, authed_client, conn, "?action=removed_show_keep")
        assert re.search(
            r'class="fpill on" href="/history\?action=unkept" aria-current="page"', html
        )
        assert 'No "keep removed" events on record.' in html
        assert "removed show keep" not in html


class TestWhoReadsWhat:
    """Everyone's rows for an admin; only their own for anyone else."""

    @pytest.fixture
    def app(self, app_factory, conn):
        return app_factory(
            history_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )

    @pytest.fixture
    def clients(self, app, conn, authed_client, viewer_client):
        admin, ana = authed_client(app, conn), viewer_client(app, conn, username="ana")
        viewer_client(app, conn, username="bob")
        conn.execute("DELETE FROM audit_log")
        insert_media_item(conn, id="m1", title="Ana Film", plex_rating_key="m1")
        insert_media_item(conn, id="m2", title="Bob Film", plex_rating_key="m2")
        log_audit(conn, "m1", "snoozed", "Kept for 30 days by ana", actor="ana")
        log_audit(conn, "m2", "snoozed", "Kept for 30 days by bob", actor="bob")
        security_event(conn, event="login.success", actor="bob", ip="192.0.2.9")
        conn.execute(
            "INSERT INTO played_titles (username, media_type, title, played_at)"
            " VALUES ('bob', 'movie', 'Bob Film', '2026-03-01T12:00:00+00:00')"
        )
        conn.commit()
        return admin, ana

    def _titles(self, client, **params):
        body = client.get("/api/history", params=params).json()
        return [i["title"] for i in body["items"]]

    def test_an_admin_reads_everyones_rows_and_plays(self, clients):
        assert self._titles(clients[0]) == ["Signed in", "Bob Film", "Ana Film", "Bob Film"]

    @pytest.mark.parametrize(
        "params",
        [{}, {"user": "bob"}, {"q": "film"}, {"per_page": 1}, {"action": "kept"}, {"page": 1}],
    )
    def test_a_viewer_reads_only_their_own(self, clients, params):
        assert self._titles(clients[1], **params) == ["Ana Film"]

    def test_a_viewers_page_holds_nothing_of_anyone_else(self, clients, conn):
        other = conn.execute("SELECT id FROM audit_log WHERE actor = 'bob'").fetchone()[0]

        html = clients[1].get(f"/history?event={other}&user=bob").text

        assert "Ana Film" in html
        assert "Bob Film" not in html
        assert "bob" not in html
        assert "192.0.2.9" not in html
        assert " open>" not in html

    def test_a_viewers_filters_are_those_of_what_they_can_do(self, clients):
        own, every = (
            re.findall(r'href="/history\?action=(\w+)"', c.get("/history").text)
            for c in reversed(clients)
        )
        assert {"kept", "plays", "security"} <= set(own)
        assert not {"deleted", "scheduled", "delete_failed"} & set(own)
        assert "deleted" in every

    def test_the_security_events_stay_with_admins(self, clients):
        assert clients[1].get("/api/security-events").status_code == 403

    @pytest.mark.parametrize(
        ("params", "titles"),
        [
            ({"user": "bob", "action": "kept"}, ["Bob Film"]),
            ({"action": "plays"}, ["Bob Film"]),
            ({"title": "ana"}, ["Ana Film"]),
            ({"q": "ana"}, ["Ana Film"]),
            ({"until": "2026-03-01"}, ["Bob Film"]),
            ({"since": "2026-03-02", "action": "plays"}, []),
        ],
    )
    def test_an_admin_searches_and_filters(self, clients, params, titles):
        assert self._titles(clients[0], **params) == titles

    def test_the_cursor_leads_on_to_the_end(self, clients):
        first = clients[0].get("/api/history", params={"per_page": 3}).json()
        rest = clients[0].get("/api/history", params={"cursor": first["next_cursor"]}).json()

        assert (first["total"], len(first["items"])) == (4, 3)
        assert len(rest["items"]) == 1
        assert rest["next_cursor"] is None

    def test_a_cursor_never_made_here_is_refused(self, clients):
        resp = clients[1].get("/api/history", params={"cursor": "nonsense"})
        assert (resp.status_code, resp.json()["error"]) == (400, "bad_cursor")

    def test_numbered_pages_follow_each_other_past_the_second(self, clients):
        seen = [self._titles(clients[0], per_page=1, page=n) for n in range(1, 6)]
        assert seen == [["Signed in"], ["Bob Film"], ["Ana Film"], ["Bob Film"], []]

    def test_a_far_page_starts_where_the_one_before_it_ended(self, clients, conn):
        conn.executemany(
            "INSERT INTO audit_log (media_item_id, action, created_at)"
            " VALUES ('m1', 'deleted', '2026-04-01T12:00:00+00:00')",
            [()] * 301,
        )
        params = {"action": "deleted", "per_page": 100, "page": 4}
        assert self._titles(clients[0], **params) == ["Ana Film"]

    def test_an_event_is_found_on_its_page_among_others_of_its_moment(self, clients, conn):
        for _ in range(3):
            insert_audit_log(
                conn, media_item_id="m1", action="deleted", created_at="2026-04-01T12:00:00+00:00"
            )
        for [event_id] in conn.execute("SELECT id FROM audit_log").fetchall():
            body = clients[0].get("/api/history", params={"event": event_id, "per_page": 1}).json()
            assert body["open_id"] == event_id
            assert [i["id"] for i in body["items"]] == [event_id]

    def test_an_event_the_filter_hides_is_not_opened(self, clients, conn):
        [kept] = conn.execute("SELECT id FROM audit_log WHERE actor = 'ana'").fetchone()
        params = {"event": kept, "action": "plays"}
        assert clients[0].get("/api/history", params=params).json()["open_id"] is None

    def test_an_account_has_its_avatar_and_a_name_typed_at_sign_in_has_none(self, clients, conn):
        security_event(conn, event="login.failed", actor="bob")
        failed, signed_in = clients[0].get("/api/history").json()["items"][:2]
        assert failed["avatar"] is None
        assert signed_in["avatar"]

    def test_one_device_signed_out_has_its_label(self, clients, conn):
        security_event(conn, event="session.revoke_one", actor="ana")
        assert self._titles(clients[1])[0] == "Device signed out"
