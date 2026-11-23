"""The old Search, Library and Recommended pages send a signed-in person to the Watch page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.recommended.pages import router as recommended_router
from mediaman.web.routes.search.page import router as search_router


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
    assert resp.headers["location"].endswith("/login")


@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("?q=dune", "/watch?q=dune"),
        ("?q=a%20%26%20b%3Dc", "/watch?q=a+%26+b%3Dc"),
        ("?q=%20%20", "/watch"),
        ("", "/watch"),
    ],
)
def test_search_keeps_a_usable_query(make, query, target):
    resp = make(search_router).get("/search" + query, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == target


def test_search_cuts_a_query_that_is_too_long(make):
    resp = make(search_router).get("/search?q=" + "x" * 500, follow_redirects=False)

    assert resp.headers["location"] == "/watch?q=" + "x" * 100


def test_library_keeps_every_valid_filter(make):
    resp = make(library_router).get(
        "/library?q=dune&type=tv&sort=size_desc&page=3&per_page=50", follow_redirects=False
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == (
        "/watch?view=list&q=dune&type=tv&sort=size_desc&page=3&per_page=50"
    )


def test_library_drops_values_the_list_would_refuse(make):
    resp = make(library_router).get(
        "/library?type=bogus&sort=bogus&page=0&per_page=9999&q=", follow_redirects=False
    )

    assert resp.headers["location"] == "/watch?view=list"


def test_library_with_no_filters_opens_the_list(make):
    resp = make(library_router).get("/library", follow_redirects=False)

    assert resp.headers["location"] == "/watch?view=list"


def test_recommended_opens_watch(make):
    resp = make(recommended_router).get("/recommended", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/watch"
