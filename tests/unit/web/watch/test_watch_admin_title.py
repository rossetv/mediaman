"""A library title's size, dates, deletion and keeps, for admins only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import catalog_api
from tests.helpers.factories import (
    insert_admin_user,
    insert_kept_show,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)

GIB = 1024**3


@pytest.fixture
def library(conn):
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(
        conn, id="100", plex_rating_key="100", title="The Glasshouse", file_size_bytes=4 * GIB
    )
    for season, key in ((1, "501"), (2, "502")):
        insert_media_item(
            conn,
            id=key,
            plex_rating_key=key,
            title=f"Season {season}",
            show_title="Glass Coast",
            media_type="tv_season",
            season_number=season,
            show_rating_key="500",
            file_size_bytes=GIB,
        )
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hidden", plex_library_id=9)
    return conn


def _client(app_factory, conn, role):
    insert_admin_user(conn, username=role, role=role)
    client = TestClient(app_factory(catalog_api.router, conn=conn))
    client.cookies.set("session_token", create_session(conn, role))
    return client


@pytest.fixture
def admin(app_factory, library):
    return _client(app_factory, library, "admin")


def _facts(client, ref):
    resp = client.get(f"/api/watch/admin/title/{ref}")
    assert resp.status_code == 200
    return resp.json()


def test_a_viewer_is_refused(app_factory, library):
    viewer = _client(app_factory, library, "viewer")

    resp = viewer.get("/api/watch/admin/title/movie-100")

    assert resp.status_code == 403
    assert "size" not in resp.text


@pytest.mark.parametrize("ref", ["movie-900", "tmdb-movie-12", "rec-1", "movie-x", "show-100"])
def test_a_title_not_in_the_scanned_libraries_or_not_a_library_ref_is_404(admin, ref):
    assert admin.get(f"/api/watch/admin/title/{ref}").status_code == 404


def test_a_movie_leaving_and_not_kept_can_be_deleted(admin, library):
    insert_scheduled_action(library, media_item_id="100")

    body = _facts(admin, "movie-100")

    assert body["size_bytes"] == 4 * GIB
    assert body["size"].startswith("4")
    assert body["deletion"]["days"] in (13, 14)
    assert body["keep"]["state"] == "none"
    assert body["actions"]["delete"] == {"kind": "movie", "id": "100", "title": "The Glasshouse"}
    assert body["actions"]["keep_wrapper"]["id"] == "100"


def test_a_kept_movie_offers_no_delete(admin, library):
    later = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    insert_scheduled_action(library, media_item_id="100", action="snoozed", execute_at=later)

    body = _facts(admin, "movie-100")

    assert body["keep"]["state"] == "kept"
    assert body["actions"]["delete"] is None


def test_the_keep_control_is_given_the_keep_as_read_to_send_it_back(admin, library):
    later = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    made = datetime(2026, 11, 1, tzinfo=UTC).isoformat()
    assert _facts(admin, "movie-100")["actions"]["keep_wrapper"]["keep"] is None

    insert_scheduled_action(library, media_item_id="100", action="snoozed", execute_at=later)
    library.execute("UPDATE scheduled_actions SET snoozed_at = ?", (made,))
    library.commit()

    keep = _facts(admin, "movie-100")["actions"]["keep_wrapper"]["keep"]
    assert keep == {"action": "snoozed", "execute_at": later, "kept_at": made}


def test_a_show_sums_its_seasons_and_deletes_through_the_season_picker(admin):
    body = _facts(admin, "show-500")

    assert body["size_bytes"] == 2 * GIB
    assert body["deletion"] is None
    assert body["actions"]["delete"] == {
        "kind": "seasons",
        "show_rk": "500",
        "title": "Glass Coast",
    }
    assert body["actions"]["keep_wrapper"]["is_tv"] is True


def test_a_show_kept_as_a_whole_reads_show_kept(admin, library):
    insert_kept_show(
        library, show_rating_key="500", show_title="Glass Coast", action="protected_forever"
    )

    body = _facts(admin, "show-500")

    assert body["keep"]["state"] == "show_kept"
    assert body["actions"]["delete"] is None


def test_a_show_with_some_seasons_kept_is_not_kept(admin, library):
    insert_scheduled_action(library, media_item_id="501", action="protected_forever", token="t-501")

    body = _facts(admin, "show-500")

    assert body["keep"] == {"state": "none", "label": "1 of 2 seasons kept"}
    assert body["actions"]["delete"] is not None


def test_a_show_says_when_each_season_leaves_soonest_first(admin, library):
    now = datetime.now(UTC)
    for key, days in (("501", 9), ("502", 4)):
        insert_scheduled_action(
            library,
            media_item_id=key,
            execute_at=(now + timedelta(days=days)).isoformat(),
            token=f"t-{key}",
        )

    body = _facts(admin, "show-500")

    assert [(d["keep_id"], d["season"], d["days"]) for d in body["deletions"]] == [
        ("502", 2, 4),
        ("501", 1, 9),
    ]
    assert body["deletion"] == body["deletions"][0]
