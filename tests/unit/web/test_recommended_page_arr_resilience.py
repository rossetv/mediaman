"""GET /recommended must survive an unreachable arr stack.

Regression: when Sonarr (or Radarr) was unreachable, ``build_*_cache`` raised
``SafeHTTPError`` out of ``attach_download_states`` and the entire
recommendations page returned 500. A recommendations page must degrade
gracefully — render without download badges — rather than hard-depend on the
arr stack being up.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.recommended.pages import router as pages_router
from tests.helpers.factories import insert_suggestion


def _arr_client_raising(*, series: bool) -> MagicMock:
    """Return a fake arr client whose library fetch raises SafeHTTPError."""
    client = MagicMock()
    error = SafeHTTPError(503, "service unavailable", "http://arr/api/v3")
    if series:
        client.get_series.side_effect = error
    else:
        client.get_movies.side_effect = error
    return client


def test_recommended_page_renders_200_when_sonarr_unreachable(
    app_factory, authed_client, conn, templates_stub
):
    """A SafeHTTPError from Sonarr must not 500 the page — it renders 200
    with the TV recommendation surfaced but lacking a download badge."""
    insert_suggestion(conn, title="Severance", media_type="tv", tmdb_id=200, category="personal")
    conn.commit()
    app = app_factory(pages_router, conn=conn, state_extras={"templates": templates_stub})
    client = authed_client(app, conn)

    with (
        patch(
            "mediaman.services.arr.build.build_sonarr_from_db",
            return_value=_arr_client_raising(series=True),
        ),
        patch("mediaman.services.arr.build.build_radarr_from_db", return_value=None),
    ):
        resp = client.get("/recommended")

    assert resp.status_code == 200
    # The recommendation still made it into the render context; the TV item
    # simply carries no live download_state (Sonarr degraded to empty).
    body = resp.json()
    recs = body["all_recommendations_json"]
    assert "Severance" in recs
    assert "download_state" not in recs


def test_recommended_page_renders_200_when_radarr_unreachable(
    app_factory, authed_client, conn, templates_stub
):
    """A SafeHTTPError from Radarr must not 500 the page either."""
    insert_suggestion(conn, title="Arrival", media_type="movie", tmdb_id=100, category="personal")
    conn.commit()
    app = app_factory(pages_router, conn=conn, state_extras={"templates": templates_stub})
    client = authed_client(app, conn)

    with (
        patch(
            "mediaman.services.arr.build.build_radarr_from_db",
            return_value=_arr_client_raising(series=False),
        ),
        patch("mediaman.services.arr.build.build_sonarr_from_db", return_value=None),
    ):
        resp = client.get("/recommended")

    assert resp.status_code == 200
    body = resp.json()
    recs = body["all_recommendations_json"]
    assert "Arrival" in recs
    assert "download_state" not in recs
