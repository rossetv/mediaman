"""The dashboard and kept-items scripts update the page in place, never reload it."""

from __future__ import annotations

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from fastapi.templating import Jinja2Templates

from mediaman.core.delete_root_state import write_delete_root_state
from mediaman.web.routes.dashboard import router as dashboard_router
from tests.helpers.factories import insert_media_item, insert_scheduled_action

_WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
_JS = _WEB / "static/js"
_HARNESS = Path(__file__).parent / "js" / "dashboard_harness.js"


class _Fragments(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.caret: dict[str, str | None] = {}
        self.menu_durations: list[str | None] = []
        self.in_roots = False
        self.roots_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-fragment" in a:
            self.names.append(a["data-fragment"] or "")
            self.in_roots = a["data-fragment"] == "roots"
        if "btn-sm-caret" in (a.get("class") or ""):
            self.caret = a
        if "snooze-option" in (a.get("class") or ""):
            self.menu_durations.append(a.get("data-duration"))

    def handle_data(self, data):
        if self.in_roots:
            self.roots_text += data


def _page(app_factory, authed_client, conn, *, scheduled: bool, roots=None) -> _Fragments:
    if roots:
        write_delete_root_state(conn, roots)
        conn.commit()
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

    def test_waiting_deletions_banner_refreshes_with_the_page(
        self, app_factory, authed_client, conn
    ):
        page = _page(app_factory, authed_client, conn, scheduled=False, roots={"/media/tv": 3})
        assert "roots" in page.names
        assert "3 scheduled deletions are waiting" in " ".join(page.roots_text.split())

    def test_banner_fragment_is_there_with_no_banner(self, app_factory, authed_client, conn):
        # A banner that appears after a scan needs a place to be swapped into.
        page = _page(app_factory, authed_client, conn, scheduled=False)
        assert "roots" in page.names
        assert page.roots_text.strip() == ""


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(_HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestRefreshInPlace:
    def test_keep_from_the_button_leaves_focus_on_the_next_tile(self):
        out = _run("focusButton")
        assert out["focused"] == {
            "id": "b",
            "tag": "BUTTON",
            "text": "Keep 30 days",
            "inPage": True,
        }

    def test_keep_from_the_menu_leaves_focus_on_the_next_tile(self):
        out = _run("focusMenu")
        assert out["focused"]["id"] == "b"
        assert out["focused"]["text"] == "Keep 30 days"
        assert out["focused"]["inPage"] is True

    def test_keep_with_motion_focuses_without_scrolling_then_reveals_the_button(self):
        out = _run("focusNoScroll")
        assert out["calls"]
        assert all(c["opts"] == {"preventScroll": True} for c in out["calls"])
        assert out["focused"]["id"] == "b"
        assert out["scrolledBefore"] == 0
        assert [s["behavior"] for s in out["scrolls"]] == ["smooth"]
        assert out["scrolls"][0]["top"] < 0

    def test_keyboard_clear_all_keeps_focus_in_the_page(self):
        out = _run("clearAll")
        assert out["busy"] == {"disabled": False, "aria": "true"}
        assert out["posts"] == 1
        assert out["focusWhileBusy"] == "BUTTON"
        assert out["focused"]["inPage"] is True
        assert out["focused"]["tag"] != "BODY"

    def test_failed_keep_moves_focus_only_out_of_the_closed_menu(self):
        out = _run("failedKeep")
        assert out["fromButton"]["id"] == "a"
        assert out["fromButton"]["text"] == "Keep 30 days"
        assert out["fromMenu"] == {"id": "b", "caret": True}

    def test_scan_finished_during_a_failed_keep_still_refreshes(self):
        assert _run("scanDuringFailedKeep")["tiles"] == ["a"]

    def test_older_refresh_answering_last_does_not_bring_a_kept_tile_back(self):
        assert _run("staleRefresh")["tiles"] == ["c"]

    def test_expired_session_sends_the_user_to_sign_in(self):
        out = _run("expired")
        assert out == {"redirected": "/login", "noFragments": "/login"}


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


class TestBannerCountFollowsKeepAndClear:
    def _banner(self, client) -> str:
        parser = _Fragments()
        parser.feed(client.get("/").text)
        return " ".join(parser.roots_text.split())

    def test_count_drops_after_a_keep_and_folder_goes_at_zero(
        self, app_factory, authed_client, conn
    ):
        ids = []
        for n in (1, 2):
            insert_media_item(conn, id=f"m{n}", title=f"F{n}", plex_rating_key=f"rk{n}")
            ids.append(insert_scheduled_action(conn, media_item_id=f"m{n}", token=f"tok{n}"))
        write_delete_root_state(conn, {"/media/tv": 2}, {"/media/tv": ids})
        conn.commit()
        app = app_factory(
            dashboard_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
        )
        client = authed_client(app, conn)
        assert "2 scheduled deletions are waiting" in self._banner(client)

        conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (ids[0],))
        conn.commit()
        assert "1 scheduled deletion is waiting" in self._banner(client)

        conn.execute("DELETE FROM scheduled_actions")
        conn.commit()
        assert "/media/tv" not in self._banner(client)

    def test_row_recorded_with_counts_only_still_renders(self, app_factory, authed_client, conn):
        write_delete_root_state(conn, {"/media/tv": 3})
        conn.commit()
        app = app_factory(
            dashboard_router,
            conn=conn,
            state_extras={"templates": Jinja2Templates(directory=str(_WEB / "templates"))},
        )
        client = authed_client(app, conn)
        assert "3 scheduled deletions are waiting" in self._banner(client)
