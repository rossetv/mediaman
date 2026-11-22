"""The Library search box, rendered with the real template.

The placeholder is not a label, so the input must carry its own accessible name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.web.routes.library import router as pages_router

_TEMPLATE_DIR = Path(__file__).parents[4] / "src" / "mediaman" / "web" / "templates"


@pytest.fixture
def client(app_factory, authed_client, conn):
    app = app_factory(
        pages_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    return authed_client(app, conn)


def test_library_search_input_is_named(client):
    html = client.get("/library").text

    (tag,) = re.findall(r"<input[^>]*name=\"q\"[^>]*>", html)
    assert 'aria-label="Search library"' in tag
