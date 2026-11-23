"""Login, first-login and unsubscribe pages: kept username, submit guard, headings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import mediaman.web
from mediaman.web.auth.password_hash import create_user
from mediaman.web.routes.auth import _limiter
from mediaman.web.routes.auth import router as auth_router

_WEB = Path(mediaman.web.__file__).parent


@pytest.fixture
def client(app_factory, conn):
    _limiter.reset()
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    app = app_factory(auth_router, conn=conn, state_extras={"templates": templates})
    create_user(conn, "admin", "correct horse battery 1", enforce_policy=False)
    return TestClient(app, raise_server_exceptions=True)


def test_failed_login_keeps_the_escaped_username_and_never_the_password(client):
    resp = client.post("/login", data={"username": '"><b>x', "password": "hunter2-secret"})

    assert 'value="&#34;&gt;&lt;b&gt;x"' in resp.text
    assert "<b>x" not in resp.text
    assert "hunter2-secret" not in resp.text


def test_failed_login_message_is_the_same_for_wrong_user_and_wrong_password(client):
    wrong_pw = client.post("/login", data={"username": "admin", "password": "nope"}).text
    wrong_user = client.post("/login", data={"username": "ghost", "password": "nope"}).text

    for text in (wrong_pw, wrong_user):
        assert "Invalid username or password." in text
    assert wrong_pw.replace("admin", "ghost") == wrong_user


def test_fresh_login_page_has_no_username_value(client):
    text = client.get("/login").text

    assert 'value="' not in text.split('id="username"')[1].split(">")[0]


def test_login_form_is_guarded_against_double_submit(client):
    text = client.get("/login").text

    assert "data-submit-guard" in text
    assert "/static/js/submit-guard.js" in text


def test_unsubscribe_pages_have_a_heading_and_no_dead_cancel_link():
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    for name, ctx in (
        ("subscribers/unsubscribe_confirm.html", {"email": "a@b.c", "token": "t"}),
        ("subscribers/unsubscribe_result.html", {"message": "m", "success": True}),
    ):
        html = templates.get_template(name).render(ctx)
        assert "<h1" in html
        assert 'href="/"' not in html


def test_rate_limited_login_keeps_the_escaped_username_and_never_the_password(client):
    for _ in range(5):
        _limiter.check("testclient")
    resp = client.post("/login", data={"username": '"><b>x', "password": "hunter2-secret"})

    assert "Too many attempts" in resp.text
    assert 'value="&#34;&gt;&lt;b&gt;x"' in resp.text
    assert "<b>x" not in resp.text
    assert "hunter2-secret" not in resp.text


def test_text_stays_readable_and_phone_inputs_do_not_trigger_zoom():
    css = _WEB / "static" / "css"
    note = (css / "_unsubscribe.css").read_text().split(".unsub-note {")[1].split("}")[0]
    assert "var(--t3)" in note
    for name, selector in (("_forms.css", ".inp"), ("_settings_pg.css", ".setg-pg .inp")):
        text = (css / name).read_text()
        pattern = (
            r"@media \(max-width: 639\.98px\) \{[^{}]*?\s"
            + re.escape(selector)
            + r" \{ font-size: 16px; \}"
        )
        assert re.search(pattern, text), name
