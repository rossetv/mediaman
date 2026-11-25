"""The app answers with the row of what a person asked for."""

from __future__ import annotations

from types import SimpleNamespace

from mediaman.db import set_connection
from mediaman.main import create_app


def test_the_app_has_the_route(authed_client, conn, secret_key):
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)

    assert authed_client(app, conn).get("/api/watch/asked").json()["items"] == []
