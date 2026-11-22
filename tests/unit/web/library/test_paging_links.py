"""Library paging links, rendered with the real template.

Search text goes into the query string, so it must be URL-encoded, not just
HTML-escaped, or "Law & Order" and "C++" break on the next page.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.web.routes.library import router as pages_router
from tests.helpers.factories import insert_media_item

_TEMPLATE_DIR = Path(__file__).parents[4] / "src" / "mediaman" / "web" / "templates"


@pytest.fixture
def client(app_factory, authed_client, conn):
    app = app_factory(
        pages_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    return authed_client(app, conn)


@pytest.mark.parametrize("search", ["Law & Order", "C++", "#1 Cheerleader Camp"])
def test_page_links_round_trip_the_search_text(client, conn, search):
    for i in range(3):
        insert_media_item(
            conn,
            id=f"m{i}",
            title=f"{search} {i}",
            media_type="movie",
            plex_rating_key=f"rk-{i}",
            file_path="/f",
            file_size_bytes=1,
        )

    page = client.get("/library", params={"q": search, "per_page": 1}).text

    hrefs = [html.unescape(h) for h in re.findall(r'class="page-btn" href="([^"]+)"', page)]
    assert hrefs
    for href in hrefs:
        assert parse_qs(urlsplit(href).query)["q"] == [search]


def _add_movies(conn, n):
    for i in range(n):
        insert_media_item(
            conn,
            id=f"p{i}",
            title=f"Movie {i}",
            media_type="movie",
            plex_rating_key=f"rk-p{i}",
            file_path="/f",
            file_size_bytes=1,
        )


def test_page_past_the_end_shows_the_last_page(client, conn):
    _add_movies(conn, 3)

    page = client.get("/library", params={"page": 9, "per_page": 2}).text

    assert "Movie" in page
    assert "3–3 of 3" in page
    assert '<button class="page-btn active">2</button>' in page


def test_empty_library_past_the_end_stays_on_page_one(client):
    page = client.get("/library", params={"page": 5}).text

    assert "Your library is empty" in page or "Connect Plex" in page


def test_sort_select_offers_recently_added_and_selects_it_by_default(client, conn):
    _add_movies(conn, 1)

    page = client.get("/library").text

    select = re.search(r'<select id="lib-mobile-sort-select".*?</select>', page, re.S).group(0)
    options = re.findall(r'<option value="(\w+)"\s*(selected)?', select)
    assert options[0] == ("added_desc", "selected")
    assert [v for v, sel in options if sel] == ["added_desc"]


def test_sort_select_selects_the_active_sort(client, conn):
    _add_movies(conn, 1)

    page = client.get("/library", params={"sort": "size_desc"}).text

    select = re.search(r'<select id="lib-mobile-sort-select".*?</select>', page, re.S).group(0)
    assert [v for v, sel in re.findall(r'<option value="(\w+)"\s*(selected)?', select) if sel] == [
        "size_desc"
    ]
