"""Tests for dashboard JSON API endpoints (stats, scheduled, deleted, reclaimed-chart)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.core.delete_root_state import write_delete_root_state
from mediaman.core.deleted_media import record_deleted_media
from mediaman.web.routes.dashboard import router as dashboard_router
from tests.helpers.factories import insert_audit_log, insert_media_item, insert_scheduled_action


def _insert_audit_deleted(conn, media_item_id: str, space_bytes: int = 500_000_000) -> None:
    insert_audit_log(
        conn, media_item_id=media_item_id, action="deleted", space_reclaimed_bytes=space_bytes
    )


def _insert_deleted_with_identity(
    conn,
    media_item_id,
    *,
    title,
    media_type="movie",
    tmdb_id=None,
    tvdb_id=None,
    season_number=None,
):
    audit_id = insert_audit_log(
        conn, media_item_id=media_item_id, action="deleted", space_reclaimed_bytes=1
    )
    record_deleted_media(
        conn,
        audit_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        season_number=season_number,
        title=title,
    )
    conn.commit()


class TestApiDashboardStats:
    def test_stats_requires_auth(self, app_factory, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 401

    def test_stats_returns_shape(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/dashboard/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "storage" in body
        assert "reclaimed_total_bytes" in body
        assert "reclaimed_total" in body
        assert body["reclaimed_total_bytes"] == 0

    def test_stats_accumulates_reclaimed(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(
            conn, id="m1", title="Dune", plex_rating_key="rk1", file_size_bytes=1_000_000
        )
        _insert_audit_deleted(conn, "m1", space_bytes=500_000_000)
        resp = client.get("/api/dashboard/stats")
        assert resp.json()["reclaimed_total_bytes"] == 500_000_000


class TestApiDashboardScheduled:
    def test_scheduled_requires_auth(self, app_factory, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 401

    def test_scheduled_empty(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_scheduled_returns_items(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(
            conn, id="m1", title="Dune", plex_rating_key="rk42", file_size_bytes=1_000_000
        )
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            execute_at=(datetime.now(UTC) + timedelta(days=7)).isoformat(),
            token="tok-m1",
        )
        resp = client.get("/api/dashboard/scheduled")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Dune"
        assert "countdown" in items[0]
        assert "file_size" in items[0]


class TestApiDashboardDeleted:
    def test_deleted_requires_auth(self, app_factory, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 401

    def test_deleted_empty(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_deleted_returns_items(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(
            conn,
            id="m1",
            title="Interstellar",
            plex_rating_key="rk99",
            file_size_bytes=1_000_000,
        )
        _insert_audit_deleted(conn, "m1")
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Interstellar"
        assert "reclaimed" in items[0]
        assert "deleted_ago" in items[0]


class TestApiDashboardReclaimedChart:
    def test_chart_requires_auth(self, app_factory, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 401

    def test_chart_empty(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 200
        assert resp.json() == {"weeks": []}

    def test_chart_aggregates_by_week(self, app_factory, authed_client, conn):
        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        # Two deletions in the same week
        now = datetime.now(UTC)
        for space in (100, 200):
            insert_audit_log(
                conn,
                media_item_id=f"m-{space}",
                action="deleted",
                space_reclaimed_bytes=space,
                created_at=now,
            )
        resp = client.get("/api/dashboard/reclaimed-chart")
        assert resp.status_code == 200
        weeks = resp.json()["weeks"]
        assert len(weeks) == 1
        assert weeks[0]["reclaimed_bytes"] == 300
        assert re.match(r"\d{4}-W\d{2}", weeks[0]["week"])
        assert isinstance(weeks[0]["reclaimed"], str)


class TestDashboardRedownload:
    """The redownload button must pass media_item_id and media_type."""

    def test_dashboard_item_includes_media_type(self, app_factory, authed_client, conn):
        insert_media_item(
            conn,
            id="m1",
            title="Test",
            plex_rating_key="rk1",
            file_path="/f",
            file_size_bytes=0,
            added_at="2024-01-01",
        )
        insert_audit_log(
            conn,
            media_item_id="m1",
            action="deleted",
            detail="detail",
            space_reclaimed_bytes=1024,
            created_at="2024-06-01",
        )

        app = app_factory(dashboard_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/dashboard/deleted")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        item = next(i for i in items if i["media_item_id"] == "m1")
        assert "media_type" in item
        assert item["media_type"] == "movie"
        assert "media_item_id" in item


class TestFetchRedownloadAuditRowsBounded:
    """fetch_redownload_audit_rows must bound its scan to a recent window so
    the dashboard render does not load an ever-growing audit_log into memory."""

    def test_recent_redownload_is_returned(self, conn):
        from mediaman.web.repository.dashboard import fetch_redownload_audit_rows

        insert_audit_log(
            conn,
            media_item_id="recent",
            action="re_downloaded",
            created_at=datetime.now(UTC) - timedelta(days=30),
        )

        rows = fetch_redownload_audit_rows(conn)
        assert {row.media_item_id for row in rows} == {"recent"}

    def test_redownload_older_than_window_is_excluded(self, conn):
        from mediaman.web.repository.dashboard import (
            _REDOWNLOAD_WINDOW_DAYS,
            fetch_redownload_audit_rows,
        )

        insert_audit_log(
            conn,
            media_item_id="ancient",
            action="re_downloaded",
            created_at=datetime.now(UTC) - timedelta(days=_REDOWNLOAD_WINDOW_DAYS + 5),
        )
        insert_audit_log(
            conn,
            media_item_id="fresh",
            action="downloaded",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )

        media_ids = {row.media_item_id for row in fetch_redownload_audit_rows(conn)}
        assert "fresh" in media_ids
        assert "ancient" not in media_ids


_TEMPLATE_DIR = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates"
_AWKWARD_TEXT = """He said "Hi" & it's <b>bold</b>"""


class _DataActionElements(HTMLParser):
    """Collect the attributes of every element carrying ``data-action``."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if "data-action" in found:
            self.elements.append(found)


def _buttons(html: str, action: str) -> list[dict[str, str | None]]:
    parser = _DataActionElements()
    parser.feed(html)
    return [e for e in parser.elements if e["data-action"] == action]


class TestJsonDataAttributesRoundTrip:
    """Values encoded with tojson in data-* attributes must reach the JS intact."""

    def test_dashboard_redownload_button_carries_title_with_quotes(
        self, app_factory, authed_client, conn
    ):
        insert_media_item(
            conn,
            id="m-quote",
            title=_AWKWARD_TEXT,
            plex_rating_key="rk1",
            file_path="/f",
            file_size_bytes=0,
            added_at="2024-01-01",
        )
        _insert_deleted_with_identity(conn, "m-quote", title=_AWKWARD_TEXT, tmdb_id=603)
        app = app_factory(
            dashboard_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
        )
        resp = authed_client(app, conn).get("/")
        assert resp.status_code == 200

        (button,) = _buttons(resp.text, "redownload")
        assert json.loads(button["data-redownload"] or "") == {
            "title": _AWKWARD_TEXT,
            "tmdb_id": 603,
        }

    def test_kept_page_remove_buttons_carry_ids_with_quotes(self):
        env = Jinja2Templates(directory=str(_TEMPLATE_DIR)).env
        item = {
            "title": "t",
            "show_title": "s",
            "media_item_id": _AWKWARD_TEXT,
            "show_rating_key": _AWKWARD_TEXT,
            "plex_rating_key": "",
            "action": "protected_forever",
            "expiry": "Forever",
            "type_label": "Movie",
            "media_type": "movie",
            "file_size": "1 GB",
        }
        html = env.get_template("protected.html").render(
            username="admin",
            nav_active="kept",
            show_items=[item],
            forever_items=[item],
            snoozed_items=[item],
        )

        (show_button,) = _buttons(html, "remove-show-keep")
        assert json.loads(show_button["data-show-rk"] or "") == _AWKWARD_TEXT
        keep_buttons = _buttons(html, "remove-keep")
        assert len(keep_buttons) == 2
        for button in keep_buttons:
            assert json.loads(button["data-media-id"] or "") == _AWKWARD_TEXT


class TestReclaimedChartIsoWeeks:
    def test_new_year_week_is_one_bucket_and_labels_sort_across_years(self, conn):
        from mediaman.web.repository.dashboard import fetch_reclaimed_chart

        # Mon 30 Dec 2024 and Wed 1 Jan 2025 are both ISO week 2025-W01.
        for day, size in (("2024-12-30", 1), ("2025-01-01", 2), ("2025-01-08", 4)):
            insert_audit_log(
                conn,
                action="deleted",
                space_reclaimed_bytes=size,
                created_at=f"{day}T10:00:00+00:00",
            )

        weeks = fetch_reclaimed_chart(conn, limit=12)

        assert [(w.week, w.reclaimed_bytes) for w in weeks] == [
            ("2025-W02", 4),
            ("2025-W01", 3),
        ]


def _dashboard_html(app_factory, authed_client, conn) -> str:
    app = app_factory(
        dashboard_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    conn.commit()
    resp = authed_client(app, conn).get("/")
    assert resp.status_code == 200
    return resp.text


class TestRedownloadButtonByStoredId:
    def test_movie_button_posts_title_and_tmdb_id(self, app_factory, authed_client, conn):
        _insert_deleted_with_identity(conn, "gone", title="Dune", tmdb_id=438631)
        html = _dashboard_html(app_factory, authed_client, conn)
        (button,) = _buttons(html, "redownload")
        assert json.loads(button["data-redownload"] or "") == {"title": "Dune", "tmdb_id": 438631}

    def test_season_button_posts_tvdb_id_and_season(self, app_factory, authed_client, conn):
        _insert_deleted_with_identity(
            conn, "gone", title="Severance", media_type="season", tvdb_id=371980, season_number=2
        )
        html = _dashboard_html(app_factory, authed_client, conn)
        (button,) = _buttons(html, "redownload")
        assert json.loads(button["data-redownload"] or "") == {
            "title": "Severance",
            "tvdb_id": 371980,
            "season_number": 2,
        }

    def test_no_button_when_stored_row_lacks_the_id(self, app_factory, authed_client, conn):
        _insert_deleted_with_identity(conn, "a", title="NoTmdb", tmdb_id=None)
        _insert_deleted_with_identity(
            conn, "b", title="NoSeason", media_type="season", tvdb_id=5, season_number=None
        )
        _insert_deleted_with_identity(
            conn, "c", title="NoTvdb", media_type="season", tvdb_id=None, season_number=1
        )
        html = _dashboard_html(app_factory, authed_client, conn)
        assert _buttons(html, "redownload") == []
        assert "NoTmdb" in html

    def test_old_audit_row_renders_without_button(self, app_factory, authed_client, conn):
        insert_media_item(conn, id="old", title="Old Film", plex_rating_key="rk1")
        _insert_audit_deleted(conn, "old")
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "Old Film" in html
        assert _buttons(html, "redownload") == []


def _record_redownload(conn, token: str) -> None:
    insert_audit_log(
        conn,
        media_item_id=token,
        action="re_downloaded",
        created_at=datetime.now(UTC) + timedelta(hours=1),
    )
    conn.commit()


class TestRedownloadedItemLeavesRecentlyDeleted:
    def test_movie_redownloaded_after_deletion_is_not_listed(
        self, app_factory, authed_client, conn
    ):
        _insert_deleted_with_identity(conn, "gone", title="Dune", tmdb_id=438631)
        _insert_deleted_with_identity(conn, "other", title="Arrival", tmdb_id=329865)
        _record_redownload(conn, "tmdb:438631")
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "Dune" not in html
        assert "Arrival" in html

    def test_season_redownloaded_after_deletion_is_not_listed(
        self, app_factory, authed_client, conn
    ):
        _insert_deleted_with_identity(
            conn, "gone", title="Severance", media_type="season", tvdb_id=371980, season_number=2
        )
        _record_redownload(conn, "tvdb:371980")
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "Severance" not in html

    def test_redownloading_one_season_keeps_the_other_seasons_button(
        self, app_factory, authed_client, conn
    ):
        for season in (1, 2):
            _insert_deleted_with_identity(
                conn,
                f"s{season}",
                title="Severance",
                media_type="season",
                tvdb_id=371980,
                season_number=season,
            )
        _record_redownload(conn, "tvdb:371980:s1")
        html = _dashboard_html(app_factory, authed_client, conn)
        (button,) = _buttons(html, "redownload")
        assert json.loads(button["data-redownload"] or "")["season_number"] == 2

    def test_movie_deleted_again_after_a_redownload_is_listed(
        self, app_factory, authed_client, conn
    ):
        insert_audit_log(
            conn,
            media_item_id="tmdb:438631",
            action="re_downloaded",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        _insert_deleted_with_identity(conn, "gone", title="Dune", tmdb_id=438631)
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "Dune" in html


class TestDeleteRootBanner:
    def test_banner_names_root_and_count(self, app_factory, authed_client, conn):
        write_delete_root_state(conn, {"/media/movies": 3, "/media/tv": 1})
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "root-banner" in html
        assert (
            "/media/movies</span> is empty or not mounted: 3 scheduled deletions are waiting"
            in html
        )
        assert "/media/tv</span> is empty or not mounted: 1 scheduled deletion is waiting" in html

    def test_no_banner_when_state_is_empty(self, app_factory, authed_client, conn, monkeypatch):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", "/media/movies")
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "root-banner" not in html

    def test_banner_escapes_paths(self, app_factory, authed_client, conn):
        write_delete_root_state(conn, {"/m/<script>alert(1)</script>": 2})
        html = _dashboard_html(app_factory, authed_client, conn)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_page_load_logs_nothing_about_delete_roots(
        self, app_factory, authed_client, conn, monkeypatch, caplog
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", ",:")
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at)"
            " VALUES ('delete_allowed_roots', 'not json', 0, '2026-01-01')"
        )
        with caplog.at_level("DEBUG"):
            _dashboard_html(app_factory, authed_client, conn)
        assert not [
            r
            for r in caplog.records
            if "DELETE_ROOTS" in r.getMessage().upper() or "delete_roots" in r.getMessage()
        ]


class TestCachedDiskUsage:
    def _setup(self, monkeypatch):
        from mediaman.web.routes.dashboard import _data

        clock = [1000.0]
        calls: list[str] = []

        def fake_usage(key):
            calls.append(key)
            return {"total_bytes": len(calls)}

        monkeypatch.setattr(_data.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(_data, "get_aggregate_disk_usage", fake_usage)
        monkeypatch.setattr(_data, "_disk_usage_cache", {})
        return _data, clock, calls

    def test_value_is_cached_within_the_ttl(self, monkeypatch):
        data, clock, calls = self._setup(monkeypatch)
        data._cached_disk_usage("/m")
        clock[0] += 29
        data._cached_disk_usage("/m")
        assert len(calls) == 1

    def test_value_is_recomputed_after_the_ttl(self, monkeypatch):
        data, clock, calls = self._setup(monkeypatch)
        data._cached_disk_usage("/m")
        clock[0] += 31
        assert data._cached_disk_usage("/m") == {"total_bytes": 2}

    def test_expired_value_is_recomputed_once_not_on_every_render(self, monkeypatch):
        data, clock, calls = self._setup(monkeypatch)
        data._cached_disk_usage("/m")
        clock[0] += 31
        data._cached_disk_usage("/m")
        data._cached_disk_usage("/m")
        data._cached_disk_usage("/m")
        assert len(calls) == 2


class TestScheduledSeasonRow:
    def test_season_row_shows_show_badge_and_season_suffix(self, conn):
        from mediaman.web.routes.dashboard._data import _fetch_scheduled

        insert_media_item(
            conn,
            id="sea1",
            title="Show S3",
            media_type="anime_season",
            plex_rating_key="rk-sea1",
            show_title="Show",
            season_number=3,
            file_path="/p",
            file_size_bytes=1,
        )
        insert_scheduled_action(
            conn,
            media_item_id="sea1",
            action="scheduled_deletion",
            execute_at=(datetime.now(UTC) + timedelta(days=5, minutes=5)).isoformat(),
        )

        [row] = _fetch_scheduled(conn)

        assert row["type_label"] == "ANIME \u00b7 S3"
        assert row["badge_class"] == "badge-anime"


class TestScheduledSeasonPill:
    def test_tv_season_pill_uses_the_tv_colour(self, app_factory, authed_client, conn):
        insert_media_item(
            conn,
            id="sea2",
            title="Show S3",
            media_type="tv_season",
            plex_rating_key="rk-sea2",
            show_title="Show",
            season_number=3,
            file_path="/p",
            file_size_bytes=1,
        )
        insert_scheduled_action(
            conn,
            media_item_id="sea2",
            action="scheduled_deletion",
            execute_at=(datetime.now(UTC) + timedelta(days=5)).isoformat(),
        )

        html = _dashboard_html(app_factory, authed_client, conn)

        assert re.search(r'class="pill pill--tv">\s*TV · S3', html)


class TestDeletionsOffNotice:
    _NOTICE = "Deletions are off: set MEDIAMAN_DELETE_ROOTS"

    def test_notice_shown_when_no_delete_folders_are_set(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        html = _dashboard_html(app_factory, authed_client, conn)
        assert self._NOTICE in html

    def test_notice_hidden_when_env_sets_delete_folders(
        self, app_factory, authed_client, conn, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        html = _dashboard_html(app_factory, authed_client, conn)
        assert self._NOTICE not in html

    def test_notice_hidden_when_setting_row_sets_delete_folders(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('delete_allowed_roots', '[\"/media/tv\"]', 0, '2026-01-01')"
        )
        conn.commit()
        html = _dashboard_html(app_factory, authed_client, conn)
        assert self._NOTICE not in html
