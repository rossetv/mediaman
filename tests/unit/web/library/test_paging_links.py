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
