"""get_current_admin refuses non-admins on its own; get_current_user lets viewers in."""

from __future__ import annotations

from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.kept import router as kept_router


def test_a_viewer_is_refused_by_an_admin_api_without_the_guard(app_factory, conn, viewer_client):
    client = viewer_client(app_factory(kept_router, conn=conn), conn)

    assert client.get("/api/kept").status_code == 403


def test_an_account_with_an_unknown_role_is_refused_by_an_admin_api(
    app_factory, conn, viewer_client
):
    client = viewer_client(app_factory(kept_router, conn=conn), conn)
    conn.execute("UPDATE admin_users SET role = 'mystery'")
    conn.commit()

    assert client.get("/api/kept").status_code == 403


def test_an_admin_passes_an_admin_api(app_factory, conn, authed_client):
    client = authed_client(app_factory(kept_router, conn=conn), conn)

    assert client.get("/api/kept").status_code == 200


def test_a_viewer_still_reaches_a_viewer_route_without_the_guard(app_factory, conn, viewer_client):
    client = viewer_client(app_factory(downloads_router, conn=conn), conn)

    assert client.get("/api/downloads").status_code == 200


def test_no_session_is_401_on_an_admin_api(app_factory, conn):
    from fastapi.testclient import TestClient

    client = TestClient(app_factory(kept_router, conn=conn))

    assert client.get("/api/kept").status_code == 401
