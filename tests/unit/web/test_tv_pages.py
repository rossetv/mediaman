"""The pages of paired TVs: the TV's own, which is anyone's, and the one a person enters its code on."""

from __future__ import annotations

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.web.routes.tv_page import router

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))


@pytest.fixture
def app(app_factory, conn):
    return app_factory(router, conn=conn, state_extras={"templates": _TEMPLATES})


def _set_base(conn, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, encrypted, updated_at)"
        " VALUES ('base_url', ?, 0, '2026-01-01')",
        (value,),
    )
    conn.commit()


def test_the_tvs_page_is_the_same_signed_in_or_out_and_names_nobody(app, conn, viewer_client):
    _set_base(conn, "https://mediaman.example/")
    out = TestClient(app).get("/tv")
    signed_in = viewer_client(app, conn).get("/tv")
    assert out.status_code == 200
    assert out.text == signed_in.text
    assert 'data-base="https://mediaman.example"' in out.text


@pytest.mark.parametrize("base", ["", "javascript:alert(1)", "http://[", "https://", "ftp://x"])
def test_a_base_url_that_is_no_page_is_not_given_to_the_tv(app, conn, base):
    _set_base(conn, base)
    page = TestClient(app).get("/tv")
    assert page.status_code == 200
    assert 'data-base=""' in page.text


def test_the_tvs_page_loads_nothing_but_its_own_files(app):
    page = TestClient(app).get("/tv").text
    assert "https://" not in page
    assert page.count("<script") == 1 and "/static/js/tv.js" in page
    assert page.count("<link") == 2


def test_a_code_is_entered_signed_in_and_a_viewer_may(app, conn, viewer_client):
    signed_out = TestClient(app).get("/pair", follow_redirects=False)
    assert (signed_out.status_code, signed_out.headers["location"]) == (302, "/login?next=%2Fpair")
    page = viewer_client(app, conn).get("/pair")
    assert page.status_code == 200
    assert "testserver/tv" in page.text


def test_the_address_to_open_on_the_tv_follows_the_base_url(app, conn, viewer_client):
    _set_base(conn, "https://mediaman.example")
    assert "<b>mediaman.example/tv</b>" in viewer_client(app, conn).get("/pair").text
