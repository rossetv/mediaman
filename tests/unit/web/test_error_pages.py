"""404, 500 and CSRF refusals: a styled page for browsers, the old answers for everyone else."""

from __future__ import annotations

import pytest
from fastapi import Body, HTTPException
from fastapi.testclient import TestClient

from mediaman.app_factory import create_app

_HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.fixture
def client():
    app = create_app()

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internals /srv/data/mediaman.db")

    @app.get("/api/boom")
    def api_boom():
        raise RuntimeError("secret internals")

    @app.get("/json-500")
    def json_500():
        raise HTTPException(status_code=500, detail="broken")

    @app.post("/api/secret")
    def api_secret(word: str = Body(embed=True)):
        return {}

    @app.post("/api/stored")
    def api_stored(word: str = Body(embed=True)):
        word.encode("utf-8")
        return {}

    @app.get("/api/latin")
    def api_latin():
        "€".encode("latin-1")

    @app.get("/api/own-text")
    def api_own_text():
        "a name read from disk \udcff".encode("utf-8")

    return TestClient(app, raise_server_exceptions=False)


def _assert_security_headers(resp):
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["referrer-policy"]
    assert resp.headers["cache-control"] == "no-store, private"


def test_a_missing_page_for_a_browser_is_the_styled_page_with_a_way_back(client):
    resp = client.get("/no/such/page", headers=_HTML)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found" in resp.text
    assert 'href="/"' in resp.text
    assert "/static/css/_error.css" in resp.text


def test_a_missing_page_for_a_non_html_caller_stays_json(client):
    resp = client.get("/no/such/page")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_a_refused_body_names_the_field_and_not_what_was_sent(client):
    resp = client.post("/api/secret", json={"word": ["hunter2"]})

    assert resp.status_code == 422
    (error,) = resp.json()["detail"]
    assert error["loc"] == ["body", "word"]
    assert error["msg"]
    assert "hunter2" not in resp.text


def test_text_sent_with_no_utf8_form_is_a_bad_request(client):
    resp = client.post(
        "/api/stored",
        content=b'{"word": "half \\ud800 a pair"}',
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400


def test_any_other_encoding_fault_is_still_a_crash(client):
    assert client.get("/api/latin").status_code == 500


def test_the_servers_own_text_with_no_utf8_form_is_still_a_crash(client):
    assert client.get("/api/own-text").status_code == 500


def test_a_missing_api_route_stays_json_even_for_a_browser(client):
    resp = client.get("/api/no/such/route", headers=_HTML)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_a_crash_shows_the_styled_page_and_no_internals(client):
    resp = client.get("/boom", headers=_HTML)

    assert resp.status_code == 500
    assert "Something went wrong" in resp.text
    assert "secret internals" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "mediaman.db" not in resp.text
    _assert_security_headers(resp)


def test_a_crash_for_a_non_html_caller_stays_plain_text(client):
    resp = client.get("/boom")

    assert resp.status_code == 500
    assert resp.text == "Internal Server Error"
    _assert_security_headers(resp)


def test_a_crash_under_api_stays_plain_text_even_for_a_browser(client):
    resp = client.get("/api/boom", headers=_HTML)

    assert resp.status_code == 500
    assert resp.text == "Internal Server Error"
    _assert_security_headers(resp)


def test_an_http_500_answer_is_json_with_the_security_headers(client):
    resp = client.get("/json-500")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "broken"}
    _assert_security_headers(resp)


def test_a_refused_cross_site_post_for_a_browser_is_the_styled_page(client):
    resp = client.post("/login", data={}, headers={**_HTML, "Origin": "http://evil.example"})

    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("text/html")
    assert "Request blocked" in resp.text
    assert "CSRF" not in resp.text
    assert "content-security-policy" in resp.headers


def test_a_refused_cross_site_post_for_a_non_html_caller_keeps_its_text(client):
    resp = client.post("/login", data={}, headers={"Origin": "http://evil.example"})

    assert resp.status_code == 403
    assert resp.text == "CSRF: origin mismatch"


def test_a_cookie_post_with_no_origin_is_refused_the_same_way(client):
    client.cookies.set("session_token", "x")

    page = client.post("/login", data={}, headers=_HTML)
    plain = client.post("/login", data={})

    assert page.status_code == plain.status_code == 403
    assert "Request blocked" in page.text
    assert plain.text == "CSRF: origin required for cookie-authenticated requests"


def test_a_crashed_page_offers_to_try_again_and_a_missing_one_does_not(client):
    assert 'href="">Try again<' in client.get("/boom", headers=_HTML).text
    assert "Try again" not in client.get("/no/such/page", headers=_HTML).text


def test_a_crashed_form_is_not_offered_a_retry_that_would_not_send_it(client):
    client.app.add_api_route("/crash", lambda: 1 / 0, methods=["POST"])

    resp = client.post("/crash", headers={**_HTML, "Origin": "http://testserver"})

    assert resp.status_code == 500
    assert "Something went wrong" in resp.text
    assert "Try again<" not in resp.text


def test_an_error_page_carries_the_wordmark(client):
    assert '<use href="#i-wordmark"/>' in client.get("/no/such/page", headers=_HTML).text
