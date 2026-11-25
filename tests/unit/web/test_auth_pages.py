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


@pytest.mark.parametrize(
    "data",
    [{"username": "admin"}, {"password": "x"}, {"username": "", "password": ""}, {}],
)
def test_sign_in_with_an_empty_field_shows_the_normal_error_page(client, data):
    resp = client.post("/login", data=data)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Invalid username or password." in resp.text


def test_failed_login_message_is_the_same_for_wrong_user_and_wrong_password(client):
    wrong_pw = client.post("/login", data={"username": "admin", "password": "nope"}).text
    wrong_user = client.post("/login", data={"username": "ghost", "password": "nope"}).text

    for text in (wrong_pw, wrong_user):
        assert "Invalid username or password." in text
    assert wrong_pw.replace("admin", "ghost") == wrong_user


def test_fresh_login_page_has_no_username_value(client):
    text = client.get("/login").text

    assert 'value="' not in text.split('id="username"')[1].split(">")[0]


def test_login_page_forgets_what_the_tab_remembers_of_a_person(client):
    assert "/static/js/signed-out.js" in client.get("/login").text


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
    share = (css / "_share.css").read_text()
    text = share.split(".share-note p {")[1].split("}")[0]
    small = share.split(".share-note .share-note__small {")[1].split("}")[0]
    assert "var(--t2)" in text and "color" not in small
    for name, selector in (("_forms.css", ".inp"), ("_settings_pg.css", ".setg-pg .inp")):
        text = (css / name).read_text()
        pattern = (
            r"@media \(max-width: 639\.98px\) \{[^{}]*?\s"
            + re.escape(selector)
            + r" \{ font-size: 16px; \}"
        )
        assert re.search(pattern, text), name


_UNSUB_PAGES = (
    ("subscribers/unsubscribe_confirm.html", {"email": "a@b.c", "token": "t"}),
    ("subscribers/unsubscribe_result.html", {"message": "m", "success": True}),
    ("subscribers/unsubscribe_result.html", {"message": "m", "success": False}),
)


def test_unsubscribe_pages_sit_in_the_sign_in_card_with_icon_font_glyphs():
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    for name, ctx in _UNSUB_PAGES:
        html = templates.get_template(name).render(ctx)
        assert "login-card" in html
        assert "&#10003;" not in html and "&#10007;" not in html


def test_unsubscribe_confirm_keeps_the_address_on_its_own_line_and_guards_the_form():
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    html = templates.get_template(_UNSUB_PAGES[0][0]).render(_UNSUB_PAGES[0][1])

    assert "<br" not in html
    assert "data-submit-guard" in html
    assert "/static/js/submit-guard.js" in html


def test_unsubscribe_pages_open_with_the_wordmark_and_the_button_names_its_busy_state():
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    for name, ctx in _UNSUB_PAGES:
        html = templates.get_template(name).render(ctx)
        assert html.index('class="brand"') < html.index("share-note__mark") < html.index("<h1")
    confirm = templates.get_template(_UNSUB_PAGES[0][0]).render(_UNSUB_PAGES[0][1])
    assert 'data-busy-label="Unsubscribing…"' in confirm
    css = (_WEB / "static" / "css" / "_unsubscribe.css").read_text()
    button = css.split(".unsub-btn-danger {")[1].split("}")[0]
    assert "min-width" in button
    assert "opacity var(--dur-fast) var(--ease-out)" in button


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_unsubscribe_button_text_is_readable_in_every_state():
    css = (_WEB / "static" / "css" / "_unsubscribe.css").read_text()
    for state in ("", ":hover", ":active"):
        rule = css.split(f".unsub-btn-danger{state} {{")[1].split("}")[0]
        fill = re.search(r"background: (#[0-9a-f]{6}|var\(--[\w-]+\))", rule).group(1)
        if fill.startswith("var("):
            tokens = (_WEB / "static" / "css" / "_tokens.css").read_text()
            fill = re.search(rf"{re.escape(fill[4:-1])}:\s*(#[0-9a-f]{{6}});", tokens).group(1)
        ratio = 1.05 / (_luminance(fill) + 0.05)
        assert ratio >= 4.5, (state, fill, ratio)
    assert ".unsub-btn-danger:focus-visible" in css


def _eye(html: str, field: str) -> str:
    """The eye of *field*, without what names its field."""
    eye = re.search(
        rf'<button type="button" class="pwd-eye"[^>]*aria-controls="{field}".*?</button>',
        html,
        re.S,
    )
    assert eye, field
    return re.sub(r' aria-(label|controls)="[^"]*"', "", eye[0])


def test_sign_in_shows_the_password_with_the_eye_of_the_password_form(client):
    from tests.unit.web.test_force_password_change import _page

    html = client.get("/login").text
    field = re.search(r'<div class="pwd-box">\s*<input ([^>]*)>', html)

    assert _eye(html, "password") == _eye(_page(), "old_password")
    assert 'aria-label="Show password"' in html
    # still the plain field a password manager fills
    assert field and 'type="password"' in field[1] and 'autocomplete="current-password"' in field[1]
    assert "/static/css/_password.css" in html and "/static/js/show-password.js" in html
    assert "<script>" not in html and " style=" not in html
