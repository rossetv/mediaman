"""The dashboard and kept-items scripts update the page in place, never reload it."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from fastapi.templating import Jinja2Templates

from mediaman.web.routes.dashboard import router as dashboard_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action

_WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
_JS = _WEB / "static/js"


class _Fragments(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.caret: dict[str, str | None] = {}
        self.menu_durations: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-fragment" in a:
            self.names.append(a["data-fragment"] or "")
        if "btn-sm-caret" in (a.get("class") or ""):
            self.caret = a
        if "snooze-option" in (a.get("class") or ""):
            self.menu_durations.append(a.get("data-duration"))


def _page(app_factory, authed_client, conn, *, scheduled: bool) -> _Fragments:
    if scheduled:
        insert_media_item(
            conn,
            id="m1",
            title="Film",
            plex_rating_key="rk1",
            file_path="/f",
            file_size_bytes=10,
            added_at="2024-01-01",
        )
        insert_scheduled_action(conn, media_item_id="m1")
    app = app_factory(
        dashboard_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
    )
    resp = authed_client(app, conn).get("/")
    assert resp.status_code == 200
    parser = _Fragments()
    parser.feed(resp.text)
    return parser


class TestDashboardFragments:
    def test_every_part_a_scan_changes_is_a_fragment(self, app_factory, authed_client, conn):
        page = _page(app_factory, authed_client, conn, scheduled=True)
        assert {"summary", "scheduled", "deleted"} <= set(page.names)

    def test_summary_fragment_is_there_with_nothing_scheduled(
        self, app_factory, authed_client, conn
    ):
        page = _page(app_factory, authed_client, conn, scheduled=False)
        assert "summary" in page.names

    def test_more_options_button_says_what_it_does(self, app_factory, authed_client, conn):
        page = _page(app_factory, authed_client, conn, scheduled=True)
        assert page.caret["aria-label"] == "Keep for a different length of time"
        assert page.caret["title"] == "Keep for a different length of time"

    def test_menu_does_not_repeat_the_keep_button(self, app_factory, authed_client, conn):
        page = _page(app_factory, authed_client, conn, scheduled=True)
        assert page.menu_durations == ["7 days", "90 days", "forever"]


class TestNoReload:
    def test_dashboard_script_never_reloads(self):
        src = (_JS / "dashboard.js").read_text(encoding="utf-8")
        assert "location.reload" not in src

    def test_kept_items_script_never_reloads(self):
        src = (_JS / "protected.js").read_text(encoding="utf-8")
        assert "location.reload" not in src

    def test_failed_scan_button_offers_a_retry(self):
        src = (_JS / "dashboard.js").read_text(encoding="utf-8")
        assert "'Scan failed'" not in src
        assert "Retry scan" in src
