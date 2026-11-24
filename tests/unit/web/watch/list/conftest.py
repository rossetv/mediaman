"""An admin's client for the list fragment, rendered with the real templates."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.web.routes.kept import router as kept_router
from mediaman.web.routes.watch import list_page

TEMPLATE_DIR = Path(__file__).parents[5] / "src" / "mediaman" / "web" / "templates"


@pytest.fixture
def client(app_factory, authed_client, conn):
    app = app_factory(
        list_page.router,
        kept_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(TEMPLATE_DIR))},
    )
    return authed_client(app, conn)
