"""The list view's paging links and sort select, rendered with the real template.

Search text goes into the query string, so it must be URL-encoded, not just
HTML-escaped, or "Law & Order" and "C++" break on the next page.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers.factories import insert_media_item


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

    page = client.get("/watch/list", params={"q": search, "per_page": 1}).text

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

    page = client.get("/watch/list", params={"page": 9, "per_page": 2}).text

    assert "Movie" in page
    assert "3–3 of 3" in page
    assert '<button class="page-btn active" aria-current="page">2</button>' in page


def test_search_past_the_end_shows_the_last_page_of_the_matches(client, conn):
    _add_movies(conn, 2)
    for i in range(3):
        insert_media_item(
            conn,
            id=f"a{i}",
            title=f"Alien {i}",
            media_type="movie",
            plex_rating_key=f"rk-a{i}",
            file_path="/f",
            file_size_bytes=1,
        )

    page = client.get("/watch/list", params={"q": "Alien", "page": 9, "per_page": 2}).text

    assert "3–3 of 3" in page
    assert '<button class="page-btn active" aria-current="page">2</button>' in page
    hrefs = [html.unescape(h) for h in re.findall(r'class="page-btn" href="([^"]+)"', page)]
    assert hrefs
    assert all(parse_qs(urlsplit(h).query)["q"] == ["Alien"] for h in hrefs)


def test_sort_select_offers_recently_added_and_selects_it_by_default(client, conn):
    _add_movies(conn, 1)

    page = client.get("/watch/list").text

    select = re.search(r'<select id="watch-list-sort".*?</select>', page, re.S).group(0)
    options = re.findall(r'<option value="(\w+)"\s*(selected)?', select)
    assert options[0] == ("added_desc", "selected")
    assert [v for v, sel in options if sel] == ["added_desc"]


def test_sort_select_selects_the_active_sort(client, conn):
    _add_movies(conn, 1)

    page = client.get("/watch/list", params={"sort": "size_desc"}).text

    select = re.search(r'<select id="watch-list-sort".*?</select>', page, re.S).group(0)
    assert [v for v, sel in re.findall(r'<option value="(\w+)"\s*(selected)?', select) if sel] == [
        "size_desc"
    ]


def test_page_links_keep_the_filter_sort_and_page_size_and_stay_on_the_list(client, conn):
    _add_movies(conn, 5)

    page = client.get(
        "/watch/list", params={"type": "movie", "sort": "size_asc", "per_page": 2}
    ).text

    hrefs = [html.unescape(h) for h in re.findall(r'class="page-btn" href="([^"]+)"', page)]
    assert hrefs
    for href in hrefs:
        parts = urlsplit(href)
        query = parse_qs(parts.query)
        assert (parts.path, query["view"]) == ("/watch", ["list"])
        assert (query["type"], query["sort"], query["per_page"]) == (["movie"], ["size_asc"], ["2"])


def test_the_range_is_shown_once(client, conn):
    _add_movies(conn, 3)

    page = client.get("/watch/list", params={"per_page": 2}).text

    assert page.count("of 3") == 1
    assert "Showing" not in page
    assert "Page 1 of 2" in page


def test_sort_select_offers_the_deletion_sorts(client, conn):
    _add_movies(conn, 1)

    page = client.get("/watch/list", params={"sort": "deletion_asc"}).text

    select = re.search(r'<select id="watch-list-sort".*?</select>', page, re.S).group(0)
    options = re.findall(r'<option value="(\w+)"\s*(selected)?', select)
    assert ("deletion_asc", "selected") in options
    assert ("deletion_desc", "") in options


def test_a_list_that_fits_on_one_page_shows_no_pager(client, conn):
    _add_movies(conn, 2)

    page = client.get("/watch/list").text

    assert "Movie 1" in page
    assert 'class="pagination"' not in page
    assert "Page 1 of 1" not in page


def _stat_hrefs(page):
    return {
        stat: html.unescape(href)
        for stat, href in re.findall(r'class="wl-stat[^"]*" data-stat="(\w+)" href="([^"]+)"', page)
    }


def test_an_active_stats_tile_clears_its_filter_and_the_others_set_theirs(client, conn):
    _add_movies(conn, 1)

    hrefs = _stat_hrefs(
        client.get("/watch/list", params={"type": "movie", "sort": "size_desc"}).text
    )

    assert parse_qs(urlsplit(hrefs["movie"]).query) == {"view": ["list"], "sort": ["size_desc"]}
    assert parse_qs(urlsplit(hrefs["tv"]).query)["type"] == ["tv"]

    stale = _stat_hrefs(client.get("/watch/list", params={"type": "stale"}).text)["stale"]
    assert "type" not in parse_qs(urlsplit(stale).query)
