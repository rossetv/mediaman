"""The Profile page holds a person's own email, password and sign-ins; Settings only points to it."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.services.avatars import drawn
from mediaman.web.auth.reauth import grant_recent_reauth
from mediaman.web.repository import avatars
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.settings import router as settings_router

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))


class _Page(HTMLParser):
    """Record each form's submit buttons, every id, and the forms' inputs by their id."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, list[str]] = {}
        self._form: str | None = None
        self.ids: set[str] = set()
        self.inputs: dict[str, str | None] = {}
        self.hints: dict[str | None, dict[str, str | None]] = {}
        self.rules: dict[str | None, list[str]] = {}
        self.scripts: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "li" and a.get("data-rule"):
            self.rules.setdefault(self._form, []).append(a["data-rule"])
        if tag == "input" and a.get("autocomplete") in ("username", "new-password"):
            self.hints.setdefault(self._form, {})[a["autocomplete"]] = a.get("value")
        if tag == "script" and a.get("src"):
            self.scripts.add(a["src"].split("?")[0])
        if tag == "form":
            self._form = a.get("id")
            self.forms[self._form] = []
        elif tag == "button" and self._form and a.get("type") == "submit":
            self.forms[self._form].append(a.get("id"))
        elif tag == "input" and self._form and a.get("id"):
            self.inputs[a["id"]] = a.get("value")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


@pytest.fixture
def settings_html(app_factory, authed_client, conn) -> str:
    app = app_factory(settings_router, conn=conn, state_extras={"templates": _TEMPLATES})
    return authed_client(app, conn).get("/settings").text


def test_an_admin_manages_their_email_password_and_sign_ins_on_profile(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    conn.execute("UPDATE admin_users SET email = 'admin@example.com'")
    conn.commit()

    page = _parse(client.get("/account").text)

    # each form is sent with Enter by its one submit button
    assert page.forms["account-email-form"] == ["account-submit-email"]
    assert page.inputs["account-email"] == "admin@example.com"
    assert page.forms["account-password-form"] == ["account-submit-password"]
    assert {"account-old-password", "account-new-password"} <= set(page.inputs)
    assert "account-revoke-others" in page.ids


@pytest.mark.parametrize(
    ("router", "path", "form", "rules"),
    [
        (
            account_router,
            "/account",
            "account-password-form",
            ["length", "mix", "guess", "same", "common"],
        ),
        (settings_router, "/settings", "add-user-form", ["length", "mix", "guess", "common"]),
    ],
)
def test_a_form_with_a_new_password_shows_the_rules_it_is_held_to(
    app_factory, authed_client, conn, router, path, form, rules
):
    app = app_factory(router, conn=conn, state_extras={"templates": _TEMPLATES})

    page = _parse(authed_client(app, conn).get(path).text)

    assert page.rules == {form: rules}
    assert "new-password" in page.hints[form]
    assert "/static/js/password-form.js" in page.scripts


def test_a_password_manager_is_told_whose_password_is_changed(app_factory, authed_client, conn):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    page = _parse(authed_client(app, conn).get("/account").text)

    assert page.hints["account-password-form"]["username"] == "admin"


def test_the_row_says_when_the_password_was_changed_if_it_is_known(
    app_factory, authed_client, conn, monkeypatch
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)

    def row() -> str:
        html = client.get("/account").text
        return html.split('id="account-password-sub">', 1)[1].split("</div>", 1)[0]

    assert row() == "<span>A new one signs out your other devices and your TVs.</span>"
    changed = {"at": "2026-03-14T09:26:53+00:00", "text": "14 March 2026"}
    monkeypatch.setattr("mediaman.web.routes.account._password_changed", lambda *_args: changed)
    assert row().startswith("Changed <time")
    assert 'datetime="2026-03-14T09:26:53+00:00">14 March 2026</time>' in row()


def test_settings_keeps_the_team_and_no_second_account_form(settings_html):
    page = _parse(settings_html)

    assert page.forms["add-user-form"] == ["btn-submit-add-user"]
    # the menu's Log out is the only other form
    assert set(page.forms) == {"add-user-form", None}


def test_a_new_username_can_be_no_longer_than_the_server_takes(settings_html):
    assert re.search(r'<input [^>]*id="new-username" maxlength="64"', settings_html)


def test_settings_users_links_to_profile(settings_html):
    section = settings_html.split('id="setg-users"', 1)[1].split('id="user-list"', 1)[0]

    assert 'href="/account"' in section


def test_what_the_ai_service_and_admins_get_is_in_sight_before_linking(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    html = authed_client(app, conn).get("/account").text

    panel = html.split('data-plex-panel="unlinked"')[1].split("data-plex-panel")[0]
    in_sight = re.sub(r"<details.*?</details>", "", panel, flags=re.S)
    assert "AI service" in in_sight
    assert "Admins can see" in in_sight
    # said with the button too, which comes first
    terms = re.search(r'id="plex-start" aria-describedby="([\w-]+)"', panel)
    assert terms and "AI service" in panel.split(f'id="{terms[1]}"')[1].split("</ul>")[0]
    assert panel.count("AI service") == panel.count("Admins can see") == 1


def test_the_page_knows_how_long_this_sessions_password_is_good_for(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    token = client.cookies.get("session_token")
    username = conn.execute("SELECT username FROM admin_users").fetchone()["username"]

    def good_for() -> int:
        found = re.search(r'data-password-good-for="(\d+)"', client.get("/account").text)
        assert found
        return int(found[1])

    grant_recent_reauth(conn, "another-session", username)
    assert good_for() == 0
    grant_recent_reauth(conn, token, "someone-else")
    assert good_for() == 0
    grant_recent_reauth(conn, token, username, window_seconds=100)
    assert 90 <= good_for() <= 100
    grant_recent_reauth(conn, token, username, window_seconds=9000)
    assert 290 <= good_for() <= 300
    grant_recent_reauth(conn, token, username, window_seconds=-1)
    assert good_for() == 0


def test_what_the_plex_card_waits_for_is_announced(app_factory, authed_client, conn):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    html = authed_client(app, conn).get("/account").text

    assert re.search(r'<p [^>]*id="plex-wait"[^>]*aria-live="polite"', html)


def test_the_page_shows_the_person_s_own_picture_and_the_ten_to_pick_from(
    app_factory, authed_client, conn
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    user_id = conn.execute("SELECT id FROM admin_users").fetchone()["id"]

    def shown() -> tuple[str, str, bool]:
        html = client.get("/account").text
        head = re.search(r'id="pic-open".*?<img src="([^"]+)"', html, re.S)
        dialog = re.search(r'<div[^>]*id="pic-dialog"[^>]*>', html)
        choice = re.search(r'data-choice="(\d*)"', dialog[0])
        assert len(re.findall(r'type="radio" name="picture"', html)) == drawn.COUNT
        assert f'data-default="{drawn.default_choice(user_id)}"' in dialog[0]
        return head[1], choice[1], "data-photo" in dialog[0]

    assert shown() == (drawn.url(drawn.default_choice(user_id)), "", False)
    avatars.set_drawn(conn, user_id, 3)
    assert shown() == (drawn.url(3), "3", False)
    avatars.set_upload(conn, user_id, "0123456789abcdef.png")
    assert shown() == (f"/api/avatars/{user_id}?v=0123456789ab", "", True)


def test_other_peoples_plex_links_are_not_on_an_admins_profile(app_factory, authed_client, conn):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    assert "Linked Plex accounts" not in authed_client(app, conn).get("/account").text


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_the_ai_part_is_there_for_everyone_and_hidden_until_the_ai_is_known_to_be(
    app_factory, authed_client, conn, role
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
    client = authed_client(app, conn)
    conn.execute("UPDATE admin_users SET role = ?", (role,))
    conn.commit()

    html = client.get("/account").text

    assert re.search(r'<section[^>]*id="account-ai"[^>]*\bhidden>', html)
    switch = re.search(r'<button[^>]*id="account-history"[^>]*>', html).group(0)
    assert 'role="switch"' in switch and 'aria-labelledby="account-history-lbl"' in switch
    assert {"account-history-lbl", "account-clear-searches"} <= _parse(html).ids


# Each fact: the part of the fold it belongs to, and words that must all be in one line of it.
_SENT = [
    ("always", "played"),
    ("always", "recent picks"),
    ("always", "suggested before", "year", "description", "reason", "choose from"),
    ("always", "picks", "titles", "played", "led to each"),
    ("always", "stars in Plex", "own", "link"),
    ("always", "in search", "question", "title and year", "more like"),
    ("always", "narrow", "choice", "titles it already gave"),
    ("history", "picks", "searches", "ratings", "requests", "plays went"),
    ("history", "questions", "daily phrases", "searches", "plays", "liked", "disliked"),
    ("never", "account name"),
    ("never", "marked as watched", "not"),
]


def _ai_text(
    app_factory, authed_client, conn, settings_html, page
) -> tuple[str, dict[str, list[str]]]:
    """The words always in view beside the switch, and the fold's lines by its parts."""
    html = settings_html
    if page == "profile":
        app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})
        html = authed_client(app, conn, username="second").get("/account").text
    fold = "account-history-more" if page == "profile" else "setg-ai-more"
    short = re.search(r'id="(?:account-history|setg-ai)-sub">(.*?)</p>', html, re.S).group(1)
    body = html.split(f'id="{fold}"')[1].split("</details>")[0]
    parts = body.split('class="fold-more-head">')[1:]
    assert [p.split("</p>")[0].split()[0] for p in parts] == ["Always:", "Only", "Never:"]
    lines = {
        name: re.findall(r"<li>(.*?)</li>", part, re.S)
        for name, part in zip(("always", "history", "never"), parts, strict=True)
    }
    return short, lines


@pytest.mark.parametrize("page", ["profile", "settings"])
def test_each_thing_openai_is_sent_is_said_in_the_right_part_beside_its_switch(
    app_factory, authed_client, conn, settings_html, page
):
    short, lines = _ai_text(app_factory, authed_client, conn, settings_html, page)

    for part, *words in _SENT:
        assert any(all(w in line for w in words) for line in lines[part]), (part, words)
    # what only history sends is never said to go always
    assert not any(w in line for line in lines["always"] for w in ("searches", "ratings", "liked"))
    said = " ".join([short, *(line for part in lines.values() for line in part)])
    assert said.count("Plex’s own apps") == 1


# Each of the three sentences always in view beside a switch, by the words it must hold.
_SHORT = {
    "profile": [
        ("titles you played", "Plex’s own apps", "linked"),
        ("With this on", "searches", "ratings", "requests"),
        ("never", "account name"),
    ],
    "settings": [
        ("picks for each person", "new and popular", "your key"),
        ("never", "account name"),
        ("stops both kinds of picks", "still ask in search"),
    ],
}


@pytest.mark.parametrize("page", ["profile", "settings"])
def test_the_three_sentences_beside_each_switch_say_the_most_of_it(
    app_factory, authed_client, conn, settings_html, page
):
    short, _ = _ai_text(app_factory, authed_client, conn, settings_html, page)

    sentences = short.split(". ")
    assert len(sentences) == 3, sentences
    for sentence, words in zip(sentences, _SHORT[page], strict=True):
        assert all(w in sentence for w in words), (sentence, words)


def test_the_settings_switch_is_described_by_its_sentences(settings_html):
    switch = re.search(r'<span[^>]*aria-label="AI recommendations"[^>]*>', settings_html).group(0)
    assert 'aria-describedby="setg-ai-sub"' in switch


@pytest.mark.parametrize(("role", "lists"), [("admin", 2), ("viewer", 1)])
def test_only_an_admins_profile_has_a_list_of_other_peoples_tvs(
    app_factory, authed_client, conn, role, lists
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    @app.middleware("http")
    async def as_role(request, call_next):
        request.state.user_role = role
        return await call_next(request)

    html = authed_client(app, conn).get("/account").text

    # hidden until there are TVs
    assert len(re.findall(r'<section[^>]*id="account-tvs[^>]*\bhidden>', html)) == lists
    assert ('id="account-tvs-all"' in html) == (role == "admin")


@pytest.mark.parametrize(
    ("role", "ready", "says"),
    [
        ("admin", True, "Where download alerts go"),
        ("admin", False, "Set it up under Email (Mailgun) in Settings"),
        ("viewer", True, "Where download alerts go"),
        ("viewer", False, "Ask an admin"),
    ],
)
def test_the_email_row_says_when_mail_is_not_set_up(
    app_factory, authed_client, conn, role, ready, says
):
    app = app_factory(account_router, conn=conn, state_extras={"templates": _TEMPLATES})

    @app.middleware("http")
    async def as_role(request, call_next):
        request.state.user_role = role
        return await call_next(request)

    if ready:
        for key in ("mailgun_domain", "mailgun_api_key", "mailgun_from_address"):
            conn.execute(
                "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, 'x', 0, 'x')",
                (key,),
            )
    conn.commit()

    html = authed_client(app, conn).get("/account").text
    sub = re.search(r'id="account-email-sub">(.*?)</div>', html, re.S).group(1)

    assert says in sub
    assert ("Where download alerts go" in sub) == ready
    assert ("Settings" in sub) == (not ready and role == "admin")
