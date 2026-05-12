"""Tests for dashboard JSON API endpoints (stats, scheduled, deleted, reclaimed-chart)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from mediaman.web.routes.dashboard import router as dashboard_router
from tests.helpers.factories import insert_audit_log, insert_media_item, insert_scheduled_action


def _insert_audit_deleted(conn, media_item_id: str, space_bytes: int = 500_000_000) -> None:
    insert_audit_log(conn, media_item_id=media_item_id, action="deleted", space_reclaimed_bytes=space_bytes)


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
        assert len(weeks[0]["reclaimed"]) > 0
