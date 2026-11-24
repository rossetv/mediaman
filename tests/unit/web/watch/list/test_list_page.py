"""GET /watch/list, the list view's fragment:

- no session → redirect to /login
- an admin → 200 with the list's context
- sort/type/page params are clamped and sanitised
- the empty states say why the list is empty
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes.watch import list_page
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings


@pytest.fixture
def _app(app_factory, conn, templates_stub):
    return app_factory(list_page.router, conn=conn, state_extras={"templates": templates_stub})


def _insert_movie(conn, media_id: str, title: str = "Test Movie") -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        media_type="movie",
        plex_rating_key=f"rk-{media_id}",
        file_path="/f",
        file_size_bytes=1_000_000,
    )


class TestListFragment:
    def test_unauthenticated_redirects_to_login(self, _app):
        """GET /watch/list without a valid session redirects to /login."""
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.get("/watch/list", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_authenticated_returns_200(self, _app, authed_client, conn):
        client = authed_client(_app, conn)
        resp = client.get("/watch/list")
        assert resp.status_code == 200

    def test_context_includes_items_and_stats(self, _app, authed_client, conn):
        _insert_movie(conn, "m1", "Dune")
        client = authed_client(_app, conn)
        ctx = client.get("/watch/list").json()
        assert "items" in ctx
        assert "stats" in ctx
        assert len(ctx["items"]) == 1

    def test_invalid_sort_is_sanitised_to_default(self, _app, authed_client, conn):
        """An unknown sort value is silently clamped to added_desc."""
        client = authed_client(_app, conn)
        resp = client.get("/watch/list?sort=invalid_sort")
        assert resp.status_code == 200
        assert resp.json()["current_sort"] == "added_desc"

    def test_invalid_type_is_sanitised_to_empty(self, _app, authed_client, conn):
        """An unknown type value is sanitised to an empty string (show all)."""
        client = authed_client(_app, conn)
        resp = client.get("/watch/list?type=invalid_type")
        assert resp.status_code == 200
        assert resp.json()["current_type"] == ""

    def test_page_below_minimum_is_clamped_to_the_first_page(self, _app, authed_client, conn):
        for i in range(3):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        client = authed_client(_app, conn)
        resp = client.get("/watch/list?page=0&per_page=2")
        assert resp.status_code == 200
        ctx = resp.json()
        assert (ctx["page"], ctx["page_start"], ctx["page_end"]) == (1, 1, 2)

    def test_per_page_above_maximum_is_clamped_to_100(self, _app, authed_client, conn):
        for i in range(101):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        client = authed_client(_app, conn)
        resp = client.get("/watch/list?per_page=9999")
        assert resp.status_code == 200
        ctx = resp.json()
        assert (ctx["per_page"], len(ctx["items"]), ctx["total_pages"]) == (100, 100, 2)

    def test_pagination_metadata_correct(self, _app, authed_client, conn):
        """page_start, page_end, and total_pages are computed correctly."""
        for i in range(5):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        client = authed_client(_app, conn)
        ctx = client.get("/watch/list?per_page=2&page=1").json()
        assert ctx["total"] == 5
        assert ctx["total_pages"] == 3
        assert ctx["page_start"] == 1
        assert ctx["page_end"] == 2

    def test_search_query_propagated_to_context(self, _app, authed_client, conn):
        """The search query string is echoed back in the template context."""
        client = authed_client(_app, conn)
        ctx = client.get("/watch/list?q=Dune").json()
        assert ctx["q"] == "Dune"


def test_the_inline_list_takes_the_same_params_and_clamps_what_a_url_can_hold(conn):
    """/watch?view=list hands its raw query to the list; nonsense falls back, never raises."""
    for i in range(3):
        _insert_movie(conn, f"m{i}", f"Film {i}")

    ctx = list_page.list_context(
        conn,
        "admin",
        {"view": "list", "page": "abc", "per_page": "9999", "q": "  Film  ", "sort": "x"},
    )

    assert (ctx["page"], ctx["per_page"], ctx["q"], ctx["current_sort"]) == (
        1,
        100,
        "Film",
        "added_desc",
    )
    assert ctx["total"] == 3


def test_a_row_carries_the_signed_in_persons_own_mark_and_not_the_last_watched_time(conn):
    insert_settings(conn, plex_libraries='["1"]')
    for username in ("aoife", "cian"):
        insert_admin_user(conn, username=username)
    for key in ("11", "12"):
        _insert_movie(conn, key, f"Film {key}")
    mark_watched(
        conn, "aoife", [MarkTarget("11", "11", None, 0)], datetime(2026, 11, 23, tzinfo=UTC)
    )

    mine = list_page.list_context(conn, "aoife", {})["items"]
    theirs = list_page.list_context(conn, "cian", {})["items"]

    assert {i["id"]: i["watched"] for i in mine} == {"11": True, "12": False}
    assert [i["watched"] for i in theirs] == [False, False]
    assert [i["last_watched"] for i in mine] == [i["last_watched"] for i in theirs]


def _empty(client) -> str:
    html = client.get("/watch/list").text
    return re.sub(r"\s+", " ", html)


def test_a_filter_that_matches_nothing_offers_to_clear_both(client, conn):
    _insert_movie(conn, "m1", "Dune")

    html = re.sub(r"\s+", " ", client.get("/watch/list?type=kept&q=lantern").text)

    assert "No kept titles match “lantern”" in html
    assert 'href="/watch?view=list" data-list-clear>Clear search and filters</a>' in html


def test_plex_not_set_up_says_connect_plex(client, conn):
    html = _empty(client)

    assert "Connect Plex to get started" in html
    assert '<a href="/settings">Open Settings</a>' in html


def test_an_empty_library_with_plex_set_up_says_so(client, conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")

    html = _empty(client)

    assert "Your library is empty" in html
    assert "Connect Plex" not in html


class TestBulkKeepResponseOk:
    """The list's keep code must check every request, never reload blindly."""

    def test_list_scripts_do_not_use_unchecked_promise_all_or_reload(self):
        """Failed requests were once swallowed by an unconditional reload; the list
        updates in place, so neither script reloads at all."""
        for name in ("list.js", "admin.js"):
            js = pathlib.Path(f"src/mediaman/web/static/js/watch/{name}").read_text()
            bad = "Promise.all(promises).then(function () { window.location.reload(); })"
            assert bad not in js, "Old unchecked Promise.all found"
            assert "location.reload" not in js

    def test_bulk_keep_and_remove_all_report_failed_requests(self):
        """core/api.js rejects on a non-ok response; admin.js catches each request
        and collects the failures, finishing only when none failed."""
        api = pathlib.Path("src/mediaman/web/static/js/core/api.js").read_text()
        js = pathlib.Path("src/mediaman/web/static/js/watch/admin.js").read_text()

        def code(src: str) -> str:
            src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
            return re.sub(r"(?m)^\s*//.*$", "", src)

        assert re.search(r"if \(!resp\.ok\) \{[^}]*throw new APIError", code(api), re.S)
        body = code(js)
        remove_all = body[body.index("function removeAllKeeps") :]
        remove_all = remove_all[: remove_all.index("function deleteBody")]
        assert ".catch(function () { failed.push(" in remove_all
        assert "if (failed.length)" in remove_all
        keep = body[body.index("function confirmKeepDialog") :]
        keep = keep[: keep.index("function removeAllKeeps")]
        assert ".catch(function () { failed.push(" in keep
        assert "if (failed.length)" in keep
