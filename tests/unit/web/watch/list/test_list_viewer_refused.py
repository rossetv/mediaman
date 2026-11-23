"""A viewer never gets the list or the admin actions behind it.

Each route refuses on its own (no guard in front), and the viewer guard
stops the request before any route runs.
"""

from __future__ import annotations

import pytest

from mediaman.web.routes.kept import router as kept_router
from mediaman.web.routes.kept_show import router as kept_show_router
from mediaman.web.routes.library_api import router as library_api_router
from mediaman.web.routes.watch import list_page
from tests.helpers.factories import insert_media_item

_ORIGIN = {"origin": "http://testserver"}

_ADMIN_CALLS = [
    ("GET", "/api/library", None),
    ("POST", "/api/media/m1/keep", {"duration": "30 days"}),
    ("POST", "/api/media/m1/delete", None),
    ("POST", "/api/media/m1/unprotect", None),
    ("POST", "/api/show/show-rk/keep", None),
    ("POST", "/api/show/show-rk/remove", None),
    ("GET", "/api/show/show-rk/seasons", None),
]


@pytest.fixture
def viewer(app_factory, viewer_client, conn):
    insert_media_item(conn, id="m1", title="Dune", plex_rating_key="rk-m1", file_path="/f")
    app = app_factory(
        list_page.router, library_api_router, kept_router, kept_show_router, conn=conn
    )
    return viewer_client(app, conn)


def test_the_list_fragment_sends_a_viewer_back_to_watch(viewer):
    resp = viewer.get("/watch/list?type=kept", follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (303, "/watch")


@pytest.mark.parametrize(("method", "path", "form"), _ADMIN_CALLS)
def test_each_admin_route_behind_the_list_refuses_a_viewer(viewer, conn, method, path, form):
    resp = viewer.request(method, path, data=form, headers=_ORIGIN)

    assert resp.status_code == 403
    row = conn.execute("SELECT 1 FROM media_items WHERE id = 'm1'").fetchone()
    assert row is not None


@pytest.mark.parametrize(("method", "path", "form"), [("GET", "/watch/list", None), *_ADMIN_CALLS])
def test_the_viewer_guard_stops_each_before_any_route_runs(
    guarded_app, conn, viewer_client, method, path, form
):
    app, reached = guarded_app

    resp = viewer_client(app, conn).request(
        method, path, data=form, headers=_ORIGIN, follow_redirects=False
    )

    if path == "/watch/list":
        assert (resp.status_code, resp.headers["location"]) == (303, "/watch")
    else:
        assert resp.status_code == 403
    assert reached == []
