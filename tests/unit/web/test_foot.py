"""TMDB's API terms ask for a notice, in their words, and their logo: every page with the nav ends in both."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.app_factory import create_app
from mediaman.db import set_connection

_NOTICE = (
    "This product uses TMDB and the TMDB APIs but is not endorsed, certified, "
    "or otherwise approved by TMDB."
)


@pytest.fixture
def app(conn, secret_key):
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)
    app.state.db = conn
    return app


@pytest.mark.parametrize("path", ["/watch", "/account"])
def test_a_page_with_the_nav_ends_in_the_tmdb_notice_and_logo(app, conn, viewer_client, path):
    foot = viewer_client(app, conn).get(path).text.split("</main>")[1].split("</footer>")[0]

    assert _NOTICE in foot
    assert 'aria-label="TMDB"' in foot


def test_the_sign_in_page_has_no_foot(app):
    assert "<footer" not in TestClient(app).get("/login").text
