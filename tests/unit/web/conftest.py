"""Shared fixtures for ``tests/unit/web``.

The previous regime had every web test file redefine its own ``_make_app``
and ``_auth_client`` helpers — 30+ near-identical copies. This conftest
provides one canonical pair plus a couple of factory-style fixtures so
new tests don't reinvent them.

Routers are passed in by name because tests want a *minimal* app (the
real ``create_app`` lifespan validates env vars, builds middleware, etc.).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3


@pytest.fixture
def app_factory(secret_key):
    """Build a minimal FastAPI app wired to a test DB.

    Usage::

        def test_x(app_factory, conn):
            app = app_factory(my_router, conn=conn)
            ...

    The returned app has ``state.config`` and ``state.db`` populated;
    the module-level ``set_connection`` is also called so route handlers
    that reach for ``get_db()`` see *conn*. Pass extra ``state_extras``
    when a route needs more (e.g. ``db_path`` for backup-style routes).
    """

    def _build(
        *routers: APIRouter,
        conn: sqlite3.Connection,
        state_extras: dict[str, object] | None = None,
    ) -> FastAPI:
        app = FastAPI()
        for router in routers:
            app.include_router(router)
        app.state.config = Config(secret_key=secret_key)
        app.state.db = conn
        for key, value in (state_extras or {}).items():
            setattr(app.state, key, value)
        set_connection(conn)
        return app

    return _build


@pytest.fixture
def authed_client():
    """Build a `TestClient` whose cookies carry a fresh admin session.

    Usage::

        def test_x(app_factory, authed_client, conn):
            app = app_factory(some_router, conn=conn)
            client = authed_client(app, conn)
            resp = client.get("/api/something")

    With ``with_reauth=True`` an additional reauth ticket is minted and
    attached as the ``reauth`` cookie — needed by the small set of routes
    that are sticky-reauth-gated (settings writes, user mutations).
    """

    def _build(
        app: FastAPI,
        conn: sqlite3.Connection,
        *,
        username: str = "admin",
        password: str = "password1234",
        with_reauth: bool = False,
    ) -> TestClient:
        create_user(conn, username, password, enforce_policy=False)
        token = create_session(conn, username)
        if with_reauth:
            from mediaman.web.auth.reauth import grant_recent_reauth

            grant_recent_reauth(conn, token, username)
        client = TestClient(app, raise_server_exceptions=True)
        client.cookies.set("session_token", token)
        return client

    return _build


@pytest.fixture
def conn(db_path):
    """Open and initialise a fresh DB for the test, yield, then close.

    Most web tests open a DB, register it, run a few requests, and need
    a clean teardown. The autouse ``_reset_db_connection_state`` fixture
    in ``tests/conftest.py`` clears the global registration; this fixture
    is responsible for the connection lifecycle within one test.
    """
    from mediaman.db import init_db

    connection = init_db(str(db_path))
    yield connection
    connection.close()


@pytest.fixture
def freezer():
    """Freeze ``datetime.now`` (and the stdlib ``time`` family) at a fixed UTC
    instant. Tests that need a different starting point pass ``time_to_freeze``
    via ``with freezer.tick(seconds=...)`` or by re-entering with a new value.

    Usage::

        def test_lockout_releases_after_window(freezer):
            ...  # initial state
            freezer.tick(seconds=601)  # past the 10-minute window
            ...  # post-window state

    Prefer this over reaching for ``datetime.now(UTC)`` directly: tests that
    depend on the wall clock are flaky on slow CI runners and unprovable.
    """
    from freezegun import freeze_time

    with freeze_time("2026-05-01T12:00:00+00:00") as frozen:
        yield frozen


def parametrise_status_codes(*pairs: Iterable) -> pytest.MarkDecorator:
    """Convenience for ``pytest.mark.parametrize(("input", "status"), [...])``.

    Replaces the ``assert resp.status_code in (400, 422)`` shape with an
    exact check per case. Currently unused; documented here so new tests
    can opt in without re-deriving the pattern.
    """
    return pytest.mark.parametrize(("payload", "expected_status"), list(pairs))
