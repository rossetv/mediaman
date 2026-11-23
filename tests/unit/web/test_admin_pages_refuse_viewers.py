"""Admin pages turn a viewer away on their own, without the viewer guard in front."""

from __future__ import annotations

import pytest

from mediaman.web.routes.dashboard import router as dashboard_router
from mediaman.web.routes.history import router as history_router
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.settings import router as settings_router


@pytest.mark.parametrize("path", ["/", "/library", "/history", "/settings"])
def test_a_viewer_is_sent_to_watch_from_each_admin_page(app_factory, viewer_client, conn, path):
    app = app_factory(dashboard_router, library_router, history_router, settings_router, conn=conn)

    resp = viewer_client(app, conn).get(path, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/watch")


@pytest.mark.parametrize("path", ["/", "/library", "/history", "/settings"])
def test_no_session_still_goes_to_login(app_factory, conn, path):
    from fastapi.testclient import TestClient

    app = app_factory(dashboard_router, library_router, history_router, settings_router, conn=conn)

    resp = TestClient(app).get(path, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (302, "/login")
