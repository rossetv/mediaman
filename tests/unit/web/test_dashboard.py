"""Tests for dashboard JSON API endpoints (stats, scheduled, deleted, reclaimed-chart)."""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.core.delete_root_state import write_delete_root_state
from mediaman.core.deleted_media import record_deleted_media
from mediaman.web.repository.settings import write_settings
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

    def test_storage_spans_the_library_folders_saved_in_settings(
        self, app_factory, authed_client, conn, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MEDIAMAN_MEDIA_PATH", str(tmp_path / "missing"))
        saved = {"1": {"path": str(tmp_path), "threshold": 90}}
        write_settings(
            conn,
            body_dict={"disk_thresholds": saved},
            allowed_keys=["disk_thresholds"],
            secret_key="unused",
            now="2026-01-01",
        )
        client = authed_client(app_factory(dashboard_router, conn=conn), conn)

        storage = client.get("/api/dashboard/stats").json()["storage"]

        assert storage["total_bytes"] == shutil.disk_usage(tmp_path).total


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

    def test_an_item_of_a_library_not_selected_is_not_listed(
        self, app_factory, authed_client, conn
    ):
        for media_id, library in (("leaving", 1), ("staying", 2)):
            insert_media_item(
                conn, id=media_id, title=media_id, plex_rating_key=media_id, plex_library_id=library
            )
            insert_scheduled_action(
                conn, media_item_id=media_id, token=f"tok-{media_id}", token_used=False
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) "
            "VALUES ('plex_libraries', '[\"1\"]', '2026')"
        )
        conn.commit()
        client = authed_client(app_factory(dashboard_router, conn=conn), conn)

        items = client.get("/api/dashboard/scheduled").json()["items"]

        assert [item["media_item_id"] for item in items] == ["leaving"]


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
    """Collect the attributes of every element carrying ``data-action`` or ``data-remove-keep-id``."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if "data-action" in found or "data-remove-keep-id" in found:
            self.elements.append(found)


class TestJsonDataAttributesRoundTrip:
    """Values encoded with tojson in data-* attributes must reach the JS intact."""

    def test_list_view_keep_and_delete_controls_carry_values_with_quotes(self):
        env = Jinja2Templates(directory=str(_TEMPLATE_DIR)).env
        kept = {
            "id": _AWKWARD_TEXT,
            "title": _AWKWARD_TEXT,
            "is_tv": False,
            "protected": True,
            "season_count": 0,
            "show_kept": False,
            "show_rating_key": "",
            "show_title_raw": "",
            "has_rating_key": False,
            "plex_rating_key": "",
            "ref": "",
            "subtitle": "",
            "protection_label": "",
            "last_watched": "",
            "media_type": "movie",
            "file_size": "1 GB",
            "deletion_at": None,
        }
        show = {
            **kept,
            "protected": False,
            "is_tv": True,
            "season_count": 3,
            "show_rating_key": _AWKWARD_TEXT,
            "media_type": "tv",
        }
        html = env.get_template("_watch_list.html").render(
            q="",
            current_type="",
            current_sort="name_asc",
            page=1,
            per_page=20,
            total_pages=1,
            total=2,
            page_start=1,
            page_end=2,
            soon_days=3,
            plex_configured=True,
            sort_options=[("name_asc", "Name")],
            stats=type("S", (), {"__getattr__": lambda self, name: 0})(),
            items=[kept, show],
            href=lambda **_: "/watch?view=list",
        )

        parser = _DataActionElements()
        parser.feed(html)
        (remove_keep,) = [e for e in parser.elements if "data-remove-keep-id" in e]
        assert json.loads(remove_keep["data-remove-keep-id"] or "") == _AWKWARD_TEXT
        (pick_seasons,) = [
            e for e in parser.elements if e.get("data-action") == "pick-season-delete"
        ]
        assert pick_seasons["data-show-rk"] == _AWKWARD_TEXT
        assert json.loads(pick_seasons["data-title"] or "") == _AWKWARD_TEXT


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


def _items(app_factory, authed_client, conn, path: str = "deleted") -> list[dict]:
    conn.commit()
    resp = authed_client(app_factory(dashboard_router, conn=conn), conn).get(
        f"/api/dashboard/{path}"
    )
    assert resp.status_code == 200
    return resp.json()["items"]


class TestRedownloadByStoredId:
    def test_a_movie_carries_its_title_and_tmdb_id(self, app_factory, authed_client, conn):
        _insert_deleted_with_identity(conn, "gone", title=_AWKWARD_TEXT, tmdb_id=438631)
        (item,) = _items(app_factory, authed_client, conn)
        assert item["redownload"] == {"title": _AWKWARD_TEXT, "tmdb_id": 438631}

    def test_a_season_carries_its_tvdb_id_and_number(self, app_factory, authed_client, conn):
        _insert_deleted_with_identity(
            conn, "gone", title="Severance", media_type="season", tvdb_id=371980, season_number=2
        )
        (item,) = _items(app_factory, authed_client, conn)
        assert item["redownload"] == {
            "title": "Severance",
            "tvdb_id": 371980,
            "season_number": 2,
        }

    def test_nothing_to_post_when_the_stored_row_lacks_the_id(
        self, app_factory, authed_client, conn
    ):
        _insert_deleted_with_identity(conn, "a", title="NoTmdb", tmdb_id=None)
        _insert_deleted_with_identity(
            conn, "b", title="NoSeason", media_type="season", tvdb_id=5, season_number=None
        )
        _insert_deleted_with_identity(
            conn, "c", title="NoTvdb", media_type="season", tvdb_id=None, season_number=1
        )
        items = _items(app_factory, authed_client, conn)
        assert {i["title"]: i["redownload"] for i in items} == {
            "NoTmdb": None,
            "NoSeason": None,
            "NoTvdb": None,
        }

    def test_an_old_audit_row_is_listed_with_nothing_to_post(
        self, app_factory, authed_client, conn
    ):
        insert_media_item(conn, id="old", title="Old Film", plex_rating_key="rk1")
        _insert_audit_deleted(conn, "old")
        (item,) = _items(app_factory, authed_client, conn)
        assert (item["title"], item["redownload"]) == ("Old Film", None)


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
        titles = [i["title"] for i in _items(app_factory, authed_client, conn)]
        assert titles == ["Arrival"]

    def test_season_redownloaded_after_deletion_is_not_listed(
        self, app_factory, authed_client, conn
    ):
        _insert_deleted_with_identity(
            conn, "gone", title="Severance", media_type="season", tvdb_id=371980, season_number=2
        )
        _record_redownload(conn, "tvdb:371980")
        titles = [i["title"] for i in _items(app_factory, authed_client, conn)]
        assert titles == []

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
        (item,) = _items(app_factory, authed_client, conn)
        assert item["redownload"]["season_number"] == 2

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
        titles = [i["title"] for i in _items(app_factory, authed_client, conn)]
        assert titles == ["Dune"]


class TestDeleteRootsWaiting:
    def _stats(self, app_factory, authed_client, conn) -> dict:
        conn.commit()
        client = authed_client(app_factory(dashboard_router, conn=conn), conn)
        return client.get("/api/dashboard/stats").json()

    def test_every_folder_is_named_with_its_count(self, app_factory, authed_client, conn):
        write_delete_root_state(conn, {"/media/movies": 3, "/media/tv": 1})
        assert self._stats(app_factory, authed_client, conn)["unavailable_roots"] == [
            {"path": "/media/movies", "waiting": 3},
            {"path": "/media/tv", "waiting": 1},
        ]

    def test_none_when_nothing_waits(self, app_factory, authed_client, conn):
        assert self._stats(app_factory, authed_client, conn)["unavailable_roots"] == []

    def test_the_count_drops_after_a_keep_and_the_folder_goes_at_zero(
        self, app_factory, authed_client, conn
    ):
        ids = []
        for n in (1, 2):
            insert_media_item(conn, id=f"m{n}", title=f"F{n}", plex_rating_key=f"rk{n}")
            ids.append(insert_scheduled_action(conn, media_item_id=f"m{n}", token=f"tok{n}"))
        write_delete_root_state(conn, {"/media/tv": 2}, {"/media/tv": ids})

        conn.commit()
        client = authed_client(app_factory(dashboard_router, conn=conn), conn)

        def waiting():
            roots = client.get("/api/dashboard/stats").json()["unavailable_roots"]
            return {r["path"]: r["waiting"] for r in roots}

        assert waiting() == {"/media/tv": 2}
        conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (ids[0],))
        conn.commit()
        assert waiting() == {"/media/tv": 1}
        conn.execute("DELETE FROM scheduled_actions")
        conn.commit()
        assert waiting() == {}

    def test_reading_the_state_logs_nothing_about_delete_roots(
        self, app_factory, authed_client, conn, monkeypatch, caplog
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", ",:")
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at)"
            " VALUES ('delete_allowed_roots', 'not json', 0, '2026-01-01')"
        )
        with caplog.at_level("DEBUG"):
            self._stats(app_factory, authed_client, conn)
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


class TestScheduledTvSeason:
    def test_a_tv_season_is_labelled_tv_with_its_number(self, app_factory, authed_client, conn):
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

        (item,) = _items(app_factory, authed_client, conn, "scheduled")

        assert (item["type_label"], item["badge_class"]) == ("TV · S3", "badge-tv")


class TestDeletionsOff:
    def _off(self, app_factory, authed_client, conn) -> bool:
        client = authed_client(app_factory(dashboard_router, conn=conn), conn)
        return client.get("/api/dashboard/stats").json()["deletions_off"]

    def test_said_when_no_delete_folders_are_set(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        assert self._off(app_factory, authed_client, conn) is True

    def test_not_said_when_the_env_sets_delete_folders(
        self, app_factory, authed_client, conn, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        assert self._off(app_factory, authed_client, conn) is False

    def test_not_said_when_the_setting_row_sets_delete_folders(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('delete_allowed_roots', '[\"/media/tv\"]', 0, '2026-01-01')"
        )
        conn.commit()
        assert self._off(app_factory, authed_client, conn) is False
