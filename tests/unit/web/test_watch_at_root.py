"""Watch lives at the site's root; its old address sends people on there."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.db import set_connection
from mediaman.web.middleware.security_headers import SecurityHeadersMiddleware
from mediaman.web.routes.watch import pages
from tests.helpers.factories import insert_settings


@pytest.fixture
def client(app_factory, conn):
    return TestClient(app_factory(pages.router, conn=conn), follow_redirects=False)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/watch", "/"),
        ("/watch?q=dune", "/?q=dune"),
        ("/watch?all=library&type=kept&sort=size", "/?all=library&type=kept&sort=size"),
        ("/watch?q=a%20%26%20b", "/?q=a%20%26%20b"),
    ],
)
def test_the_old_address_moves_to_the_root_with_its_query(client, method, old, new):
    resp = client.request(method, old)

    assert (resp.status_code, resp.headers["location"]) == (301, new)


def test_the_root_sends_someone_signed_out_to_plain_sign_in(client):
    resp = client.get("/")

    assert (resp.status_code, resp.headers["location"]) == (302, "/login")


def test_a_search_at_the_root_is_kept_for_after_sign_in(client):
    resp = client.get("/?q=dune")

    assert resp.headers["location"] == "/login?next=%2F%3Fq%3Ddune"


@pytest.fixture
def cast_on(conn):
    set_connection(conn)
    insert_settings(conn, google_cast_enabled="true")


def _csp(path: str) -> str:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_api_route(path, lambda: None)
    return TestClient(app).get(path).headers["Content-Security-Policy"]


def _without_nonce(csp: str) -> str:
    return " ".join(part for part in csp.split(" ") if not part.startswith("'nonce-"))


def test_the_root_has_the_policy_of_a_page_that_casts(cast_on):
    assert _without_nonce(_csp("/")) == _without_nonce(_csp("/watch/show/7"))
    assert "https://www.gstatic.com/" in _csp("/")


def test_the_old_address_no_longer_lets_the_cast_script_in(cast_on):
    assert "https://www.gstatic.com/" not in _csp("/watch")
