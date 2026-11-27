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
from tests.unit.web._css import contrast, px, rgb, style, token

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
        assert "Cancel" not in html


def test_rate_limited_login_keeps_the_escaped_username_and_never_the_password(client):
    for _ in range(5):
        _limiter.check("testclient")
    resp = client.post("/login", data={"username": '"><b>x', "password": "hunter2-secret"})

    assert "Too many attempts" in resp.text
    assert 'value="&#34;&gt;&lt;b&gt;x"' in resp.text
    assert "<b>x" not in resp.text
    assert "hunter2-secret" not in resp.text


def _colour(value: str) -> tuple[float, ...]:
    """The channels of a colour declared as is or through a token."""
    return rgb(token(value[4:-1]) if value.startswith("var(") else value)


def _over(colour: str, under: tuple[float, ...]) -> tuple[float, ...]:
    """*colour* as it shows over the opaque *under*."""
    *fg, alpha = (*_colour(colour), 1.0)[:4]
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg, under, strict=True))


def test_text_stays_readable_and_phone_inputs_do_not_trigger_zoom():
    note = style(".share-note p", "_share.css")["color"]
    card = _over(style(".login-card", "_login.css")["background"], _colour("var(--bg)"))
    assert contrast(_over(note, card), card) >= 4.5
    assert "color" not in style(".share-note .share-note__small", "_share.css")
    # under 16px, iOS zooms the page when a field is focused, on a narrow screen or a wide touch one
    for selector, sheet, narrow in (
        (".inp", "_forms.css", "(max-width: 639.98px)"),
        (".setg-pg .inp", "_settings_pg.css", "(max-width: 639.98px)"),
        (".login-field input", "_login.css", "(max-width: 640px)"),
    ):
        for phone in (narrow, "(pointer: coarse)"):
            assert px(style(selector, sheet, phone)["font-size"]) >= 16, (selector, phone)


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
    # it keeps its width for the busy label, and fades into the busy state
    button = style(".unsub-btn-danger", "_unsubscribe.css")
    assert "min-width" in button
    assert "opacity" in button["transition"]


def test_unsubscribe_button_text_is_readable_in_every_state():
    text = _colour(style(".unsub-btn-danger", "_unsubscribe.css")["color"])
    for state in ("", ":hover", ":active"):
        fill = style(f".unsub-btn-danger{state}", "_unsubscribe.css")["background"]
        assert contrast(text, _colour(fill)) >= 4.5, state
    assert style(".unsub-btn-danger:focus-visible", "_unsubscribe.css")


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
