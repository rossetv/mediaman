"""Profile, History and Settings open as panes over Watch: their old addresses lead to the pane,
and with ``pane=1`` they answer the pane's fragment."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.db import set_connection
from mediaman.main import create_app
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.history import router as history_router
from mediaman.web.routes.settings import router as settings_router
from mediaman.web.routes.watch import pages as watch_pages

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATE_DIR))
_TEMPLATES.env.globals["static_v"] = "v123"
_SRC = Path(__file__).parents[3] / "src" / "mediaman"
_ORIGIN = {"origin": "http://testserver"}
_NAVIGATE = {"sec-fetch-mode": "navigate"}

_PROFILE_IDS = [
    "pic-open",
    "pic-dialog",
    "pic-save",
    "pic-error",
    "pic-kind",
    "pic-upload",
    "pic-remove",
    "pic-place",
    "pic-crop",
    "pic-zoom",
    "pic-back",
    "pic-use",
    "account-email-form",
    "account-email",
    "account-email-result",
    "account-submit-email",
    "account-open-password",
    "account-password-form",
    "account-old-password",
    "account-new-password",
    "account-submit-password",
    "account-password-live",
    "plex",
    "plex-start",
    "plex-start-stop",
    "plex-terms",
    "plex-more",
    "plex-lead",
    "plex-wait",
    "plex-open",
    "plex-cancel",
    "plex-moves",
    "plex-confirm",
    "plex-decline",
    "plex-failed",
    "plex-retry",
    "plex-retry-stop",
    "plex-read",
    "plex-unlink",
    "plex-done",
    "plex-moved",
    "plex-moved-dismiss",
    "plex-signout-note",
    "plex-error",
    "plex-status",
    "account-tvs",
    "account-tvs-all",
    "account-sessions",
    "account-revoke-others",
    "account-revoke-why",
    "account-ai",
    "account-history",
    "account-history-more",
    "account-clear-searches",
]

# A view is left by the pane's own Up, Close and Escape, through the guard's Discard, so no view
# carries a close or cancel button of its own any more.
_PROFILE_IDS_GONE = ["pic-close", "pic-cancel", "account-cancel-email", "account-cancel-password"]

_SETTINGS_IDS = [
    "setg-bootstrap",
    "setg-savebar",
    "save-status",
    "btn-discard",
    "btn-save",
    "setg-overview",
    "setg-general",
    "setg-libraries",
    "setg-cleanup",
    "setg-integrations",
    "setg-tvs",
    "setg-newsletter",
    "setg-users",
    "ov-storage-big",
    "ov-storage-bar",
    "ov-scan-big",
    "ov-scan-when",
    "ov-status",
    "base_url",
    "scan_day",
    "scan_time",
    "scan_timezone",
    "btn-sync-library",
    "library_sync_interval",
    "suggestions_enabled",
    "setg-ai-sub",
    "setg-ai-need",
    "setg-ai-more",
    "lib-pill-count",
    "plex-library-pills",
    "disk-threshold-rows",
    "min_age_days",
    "inactivity_days",
    "grace_days",
    "intg-plex",
    "intg-sonarr",
    "intg-radarr",
    "intg-nzbget",
    "intg-mailgun",
    "intg-openai",
    "intg-tmdb",
    "intg-omdb",
    "plex_url",
    "plex_token",
    "sonarr_url",
    "sonarr_public_url",
    "sonarr_api_key",
    "radarr_url",
    "radarr_public_url",
    "radarr_api_key",
    "nzbget_url",
    "nzbget_username",
    "nzbget_password",
    "mailgun_domain",
    "mailgun_from_address",
    "mailgun_api_key",
    "openai_api_key",
    "openai_model",
    "ai_questions_per_day",
    "ai_everyone_per_day",
    "btn-refresh-picks",
    "tmdb_read_token",
    "omdb_api_key",
    "tv-list-wrap",
    "cast-more",
    "cast-more-toggle",
    "cast-lets",
    "tv-needs",
    "subscriber-list",
    "new-subscriber-email",
    "btn-add-subscriber",
    "btn-send-newsletter",
    "newsletter-send-panel",
    "newsletter-recipient-list",
    "newsletter-recipient-note",
    "newsletter-base-note",
    "btn-confirm-newsletter",
    "newsletter-send-status",
    "btn-add-user",
    "add-user-form",
    "new-username",
    "new-user-role",
    "new-user-password",
    "add-user-live",
    "user-list",
]


class _Fragment(HTMLParser):
    """The fragment's root, its views, every id, and each form's method."""

    def __init__(self) -> None:
        super().__init__()
        self.root: dict[str, str | None] | None = None
        self.views: list[dict[str, str | None]] = []
        self.ids: list[str] = []
        self.forms: list[str | None] = []
        self.tags: set[str] = set()
        self.hooks: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.root is None:
            self.root = {"tag": tag, **a}
        self.tags.add(tag)
        self.hooks.update(k for k in a if k.startswith("data-hist"))
        if a.get("id"):
            self.ids.append(a["id"])
        if "you-view" in (a.get("class") or "").split():
            self.views.append(a)
        if tag == "form":
            self.forms.append(a.get("method"))


def _fragment(html: str) -> _Fragment:
    parsed = _Fragment()
    parsed.feed(html)
    return parsed


@pytest.fixture
def app(app_factory, conn):
    return app_factory(
        account_router,
        history_router,
        settings_router,
        watch_pages.router,
        conn=conn,
        state_extras={"templates": _TEMPLATES},
    )


@pytest.fixture
def admin(app, authed_client, conn) -> TestClient:
    client = authed_client(app, conn)
    client.follow_redirects = False
    return client


@pytest.fixture
def viewer(app, viewer_client, conn) -> TestClient:
    @app.middleware("http")
    async def _as_viewer(request, call_next):
        # The viewer guard puts the role on the request in the real app.
        request.state.user_role = "viewer"
        return await call_next(request)

    client = viewer_client(app, conn)
    client.follow_redirects = False
    return client


def _audit_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


@pytest.mark.parametrize(
    ("path", "pane"),
    [
        ("/account", "/?you=profile"),
        ("/account?tab=x", "/?you=profile"),
        ("/history", "/?you=history"),
        ("/settings", "/?you=settings"),
        ("/settings?pane=0", "/?you=settings"),
    ],
)
def test_each_old_address_is_sent_to_its_pane_over_watch(admin, path, pane):
    resp = admin.get(path)

    assert (resp.status_code, resp.headers["location"]) == (302, pane)


def test_history_folds_only_its_own_query_keys_into_h_in_a_fixed_order(admin):
    resp = admin.get(
        "/history?page=2&secret=1&q=a+b&user=sam&action=plays&title=Dune"
        "&since=2026-01-01&until=2026-02-01&date=week&pane=0&event=42"
    )

    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["you"] == ["history"]
    assert parse_qsl(query["h"][0]) == [
        ("q", "a b"),
        ("user", "sam"),
        ("action", "plays"),
        ("title", "Dune"),
        ("since", "2026-01-01"),
        ("until", "2026-02-01"),
        ("date", "week"),
        ("page", "2"),
        ("event", "42"),
    ]


def test_an_event_link_keeps_its_event(admin):
    resp = admin.get("/history?event=42")

    assert resp.headers["location"] == "/?you=history&h=event%3D42"


def test_a_value_in_h_cannot_add_keys_to_the_pane_address(admin):
    resp = admin.get("/history?q=x%26you%3Dsettings%23top")

    location = resp.headers["location"]
    assert "#" not in location
    query = parse_qs(urlsplit(location).query)
    assert set(query) == {"you", "h"}
    assert query["you"] == ["history"]
    assert parse_qsl(query["h"][0]) == [("q", "x&you=settings#top")]


def test_history_with_only_unknown_or_empty_keys_has_no_h(admin):
    assert admin.get("/history?q=&evil=1").headers["location"] == "/?you=history"


@pytest.mark.parametrize(
    ("path", "pane"),
    [
        ("/account?pane=1", "/?you=profile"),
        ("/history?pane=1&q=dune", "/?you=history&h=q%3Ddune"),
        ("/settings?pane=1", "/?you=settings"),
    ],
)
def test_a_fragment_opened_as_a_page_is_sent_to_its_pane(admin, path, pane):
    resp = admin.get(path, headers=_NAVIGATE)

    assert (resp.status_code, resp.headers["location"]) == (302, pane)


@pytest.mark.parametrize(
    ("path", "group", "views"),
    [
        (
            "/account?pane=1",
            "profile",
            [
                "profile",
                "profile/picture",
                "profile/email",
                "profile/password",
                "profile/plex",
                "profile/devices",
            ],
        ),
        ("/history?pane=1", "history", ["history"]),
        ("/settings?pane=1", "settings", ["settings"]),
    ],
)
def test_pane_1_answers_a_fragment_of_views_that_no_one_stores(admin, path, group, views):
    resp = admin.get(path, headers={"sec-fetch-mode": "cors"})

    assert resp.status_code == 200
    assert "no-store" in resp.headers["cache-control"]
    fragment = _fragment(resp.text)
    assert not {"html", "head", "body", "nav", "header", "main"} & fragment.tags
    assert fragment.root["tag"] == "div"
    assert fragment.root["data-you-group"] == group
    assert [v["data-view"] for v in fragment.views] == views
    assert all(v["data-title"] for v in fragment.views)
    assert resp.text.lstrip().startswith("<div data-you-group")


def test_a_fragment_names_its_assets_by_the_static_version_and_carries_no_script_tag(admin):
    for path in ("/account?pane=1", "/history?pane=1", "/settings?pane=1"):
        html = admin.get(path).text
        root = _fragment(html).root
        scripts = root["data-scripts"].split()
        assert scripts and all(re.fullmatch(r"/static/js/[\w/-]+\.js\?v=v123", s) for s in scripts)
        assert all(
            re.fullmatch(r"/static/css/\w+\.css\?v=v123", s) for s in root["data-styles"].split()
        )
        assert len(scripts) == len(set(scripts))
        assert not re.search(r"<script(?![^>]*application/json)", html)


def test_settings_loads_its_modules_after_the_helpers_and_its_bootstrap_last():
    html = _TEMPLATES.env.get_template("panes/_settings.html").render(
        settings={}, plex_libraries_selected=[], timezones=[], username="a"
    )
    scripts = [s.split("?")[0] for s in _fragment(html).root["data-scripts"].split()]

    assert scripts.index("/static/js/core/reauth.js") < scripts.index(
        "/static/js/settings/savebar.js"
    )
    assert scripts.index("/static/js/settings/savebar.js") < scripts.index(
        "/static/js/settings/general.js"
    )
    assert scripts[-1] == "/static/js/settings.js"


def test_every_id_and_hook_of_the_three_panes_stays_and_none_is_used_twice(admin):
    profile = _fragment(admin.get("/account?pane=1").text)
    history = _fragment(admin.get("/history?pane=1").text)
    settings = _fragment(admin.get("/settings?pane=1").text)

    assert set(_PROFILE_IDS) <= set(profile.ids)
    assert set(_SETTINGS_IDS) <= set(settings.ids)
    assert "history-data" in history.ids
    assert {"data-hist", "data-hist-clear", "data-hist-said"} <= history.hooks
    every = profile.ids + history.ids + settings.ids
    assert len(every) == len(set(every))


def test_a_view_has_no_way_out_of_its_own_and_the_pane_over_watch_has_up_and_close(admin):
    profile = _fragment(admin.get("/account?pane=1").text)
    watch = admin.get("/").text

    assert not set(_PROFILE_IDS_GONE) & set(profile.ids)
    shell = watch[watch.index('id="you"') :]
    assert shell.count("data-you-back") == 1
    assert re.search(r'<button[^>]*class="you__close" data-you-close aria-label="Close"', shell)


def test_no_pane_id_is_one_watch_already_uses(admin):
    watch = _fragment(admin.get("/").text)
    panes = [
        _fragment(admin.get(p).text)
        for p in ("/account?pane=1", "/history?pane=1", "/settings?pane=1")
    ]

    assert not set(watch.ids) & {i for p in panes for i in p.ids}


def test_every_form_in_a_pane_is_posted(admin):
    for path in ("/account?pane=1", "/history?pane=1", "/settings?pane=1"):
        forms = _fragment(admin.get(path).text).forms
        assert forms and all(method == "post" for method in forms), path


def test_reading_any_pane_or_its_address_writes_nothing(admin, conn):
    conn.execute("DELETE FROM audit_log")
    conn.commit()
    for path in (
        "/account",
        "/account?pane=1",
        "/history?q=x",
        "/history?pane=1&event=1",
        "/settings",
        "/settings?pane=1",
    ):
        admin.get(path)

    assert _audit_rows(conn) == 0


@pytest.mark.parametrize("path", ["/settings", "/settings?pane=1", "/settings?pane=1&x=1"])
def test_a_viewer_asking_for_settings_is_still_sent_to_watch(viewer, path):
    resp = viewer.get(path)

    assert (resp.status_code, resp.headers["location"]) == (303, "/")


def test_a_viewer_gets_their_own_profile_and_history_fragments(viewer):
    profile = viewer.get("/account?pane=1")
    history = viewer.get("/history?pane=1")

    assert profile.status_code == history.status_code == 200
    assert "account-tvs-all" not in _fragment(profile.text).ids
    assert 'class="hx is-mine"' in history.text


@pytest.mark.parametrize(
    ("path", "login"),
    [
        ("/account", "/login?next=%2Faccount"),
        ("/history?q=dune&page=2", "/login?next=%2Fhistory%3Fq%3Ddune%26page%3D2"),
        ("/settings?pane=1", "/login?next=%2Fsettings%3Fpane%3D1"),
    ],
)
def test_someone_signed_out_is_sent_to_sign_in_and_back(app, path, login):
    resp = TestClient(app, follow_redirects=False).get(path)

    assert (resp.status_code, resp.headers["location"]) == (302, login)


@pytest.fixture
def real_app(conn):
    set_connection(conn)
    return create_app()


def test_a_flagged_admin_changes_their_password_before_any_pane(real_app, conn):
    create_user(conn, "a", "password1234", enforce_policy=False, must_change_password=True)
    client = TestClient(real_app, follow_redirects=False)
    client.cookies.set("session_token", create_session(conn, "a"))

    for path in ("/settings", "/settings?pane=1", "/account?pane=1", "/history"):
        assert client.get(path).headers["location"] == "/force-password-change", path


@pytest.mark.parametrize(
    ("next_page", "landing"),
    [
        ("/?you=history&h=q%3Ddune%26event%3D4", "/?you=history&h=q%3Ddune%26event%3D4"),
        ("/?you=settings/integrations/openai", "/?you=settings/integrations/openai"),
        ("//example.com/?you=profile", "/"),
        ("/\\example.com/?you=profile", "/"),
    ],
)
def test_signing_in_returns_to_a_pane_address_and_never_off_the_site(
    real_app, conn, next_page, landing
):
    create_user(conn, "a", "Correct-Horse-9-battery", enforce_policy=False)

    resp = TestClient(real_app, follow_redirects=False).post(
        "/login",
        params={"next": next_page},
        data={"username": "a", "password": "Correct-Horse-9-battery"},
        headers=_ORIGIN,
    )

    assert (resp.status_code, resp.headers["location"]) == (302, landing)


def test_the_old_watch_address_still_leads_to_the_root_with_a_pane_address(admin):
    resp = admin.get("/watch?you=history&h=q%3Ddune")

    assert (resp.status_code, resp.headers["location"]) == (301, "/?you=history&h=q%3Ddune")


def test_no_email_links_to_a_page_that_is_now_a_pane():
    mail = [
        *(_SRC / "services/mail/newsletter/templates").glob("*.html"),
        _SRC / "services/downloads/_notification_email.py",
    ]
    text = "".join(p.read_text(encoding="utf-8") for p in mail)

    assert not re.search(r"/(account|history|settings)\b", text)
