"""An admin's client for All titles."""

from __future__ import annotations

import pytest

from mediaman.web.routes import kept, library_api
from mediaman.web.routes.watch import library_row_api, list_page

ROW = "/api/watch/library/library/row"


def tick_library(conn) -> None:
    """Library 1 is ticked in Settings, as it is already where a test has scheduled something."""
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value, updated_at)"
        " VALUES ('plex_libraries', '[\"1\"]', '2026-01-01')"
    )
    conn.commit()


@pytest.fixture
def client(app_factory, authed_client, conn):
    tick_library(conn)
    app = app_factory(
        library_row_api.router, list_page.router, kept.router, library_api.router, conn=conn
    )
    return authed_client(app, conn)


@pytest.fixture
def read(client):
    """The answer for what stands in the address after ?."""

    def _read(query: str = "") -> dict:
        resp = client.get(f"{ROW}?{query}")
        assert resp.status_code == 200
        return resp.json()

    return _read


@pytest.fixture
def cards(read):
    return lambda query="": read(query)["items"]


@pytest.fixture
def admin_of(cards):
    """What an admin's actions of the one title there is need."""

    def _admin_of() -> dict:
        (card,) = cards()
        return card["admin"]

    return _admin_of
