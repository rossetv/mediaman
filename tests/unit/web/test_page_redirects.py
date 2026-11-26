"""The old Search, Library and Recommended pages send a signed-in person to the Watch page."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.recommended.pages import router as recommended_router
from mediaman.web.routes.search.page import router as search_router
from mediaman.web.routes.watch.list_page import router as list_router


@pytest.fixture
def make(app_factory, authed_client, conn):
    def _make(router, signed_in=True):
        app = app_factory(router, conn=conn)
        return authed_client(app, conn) if signed_in else TestClient(app)

    return _make


@pytest.mark.parametrize(
    ("router", "path"),
    [
        (search_router, "/search?q=dune"),
        (library_router, "/library?q=dune"),
        (recommended_router, "/recommended"),
    ],
)
def test_a_signed_out_visitor_goes_to_login_not_to_watch(make, router, path):
    resp = make(router, signed_in=False).get(path, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?" + urlencode({"next": path})


@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("?q=dune", "/?q=dune"),
        ("?q=a%20%26%20b%3Dc", "/?q=a+%26+b%3Dc"),
        ("?q=%20%20", "/"),
        ("", "/"),
    ],
)
def test_search_keeps_a_usable_query(make, query, target):
    resp = make(search_router).get("/search" + query, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == target


def test_search_cuts_a_query_that_is_too_long(make):
    resp = make(search_router).get("/search?q=" + "x" * 500, follow_redirects=False)

    assert resp.headers["location"] == "/?q=" + "x" * 100


@pytest.mark.parametrize(
    ("router", "path"), [(library_router, "/library"), (list_router, "/watch/list")]
)
@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("?type=tv&sort=size_desc&page=3&per_page=50", "?all=library&type=tv&sort=size"),
        ("?type=movie&sort=size_asc", "?all=library&type=movies&sort=small"),
        ("?type=anime&sort=name_desc", "?all=library&type=anime&sort=az"),
        ("?type=kept&sort=deletion_desc", "?all=library&type=kept&sort=leaving"),
        ("?type=stale&sort=watched_asc", "?all=library&type=stale&sort=stalest"),
        ("?sort=watched_desc", "?all=library&sort=seen"),
        ("?sort=added_desc", "?all=library"),
        ("?q=dune%20%26&type=tv", "?q=dune+%26"),
    ],
)
def test_the_old_filters_and_sorts_lead_to_the_same_of_all_titles(
    make, router, path, query, target
):
    resp = make(router).get(path + query, follow_redirects=False)

    assert (resp.status_code, resp.headers["location"]) == (302, "/" + target)


def test_library_drops_values_there_are_none_of(make):
    resp = make(library_router).get(
        "/library?type=bogus&sort=bogus&page=0&per_page=9999&q=", follow_redirects=False
    )

    assert resp.headers["location"] == "/?all=library"


def test_library_with_no_filters_opens_all_titles(make):
    resp = make(library_router).get("/library", follow_redirects=False)

    assert resp.headers["location"] == "/?all=library"


def test_recommended_opens_watch_at_its_recommended_row(make):
    resp = make(recommended_router).get("/recommended", follow_redirects=False)

    assert resp.status_code == 302
    # No fragment of its own, so the browser keeps an old link's #recommendation-<id>.
    assert resp.headers["location"] == "/?row=recommended"
