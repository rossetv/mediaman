"""The JSON the Watch page embeds for its scripts must not be able to end or
comment out its <script> element, whatever is typed into the search."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import pages
from tests.helpers.factories import insert_admin_user, insert_suggestion

_TEMPLATES = Path(mediaman.__file__).parent / "web" / "templates"
_QUERY = "</script><!--<script>x & 'y'"
_ISLAND = re.compile(r'<script type="application/json" id="watch-page-data">(.*?)</script>', re.S)


def test_page_data_has_no_markup_characters_and_parses_to_the_same_search(app_factory, conn):
    insert_admin_user(conn, username="viewer", role="viewer")
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATES))},
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "viewer"))

    resp = client.get("/watch?q=" + quote(_QUERY, safe=""))

    assert resp.status_code == 200
    (embedded,) = _ISLAND.findall(resp.text)
    for ch in "<>&'":
        assert ch not in embedded
    assert json.loads(embedded) == {
        "is_admin": False,
        "tmdb_configured": False,
        "view": "rows",
        "q": _QUERY,
        "grids": {"recommended": "Recommended for you"},
        "grid": None,
        "settings_tmdb_href": None,
    }


def test_picks_are_not_embedded_in_the_page_so_their_text_never_reaches_the_markup(
    app_factory, conn
):
    insert_admin_user(conn, username="admin", role="admin")
    insert_suggestion(
        conn, title="<!--<script>", description="Fish & chips</script><b>bold</b>", tmdb_id=1
    )
    conn.commit()
    app = app_factory(
        pages.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATES))},
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "admin"))

    resp = client.get("/watch")

    assert resp.status_code == 200
    (embedded,) = _ISLAND.findall(resp.text)
    for ch in "<>&'":
        assert ch not in embedded
    assert "<!--<script>" not in resp.text
    assert "Fish &" not in resp.text and "<b>bold</b>" not in resp.text
