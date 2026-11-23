"""The field that searches the list, rendered with the real templates.

The placeholder is not a label, so the input must carry its own accessible name.
"""

from __future__ import annotations

import re

from fastapi.templating import Jinja2Templates

from mediaman.web.routes.watch import list_page, pages
from tests.unit.web.watch.list.conftest import TEMPLATE_DIR


def test_list_search_input_is_named(app_factory, authed_client, conn):
    app = app_factory(
        pages.router,
        list_page.router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(TEMPLATE_DIR))},
    )

    html = authed_client(app, conn).get("/watch?view=list").text

    (tag,) = re.findall(r"<input[^>]*id=\"watch-search\"[^>]*>", html)
    assert 'aria-label="Search your library"' in tag
    assert "data-list-root" in html
