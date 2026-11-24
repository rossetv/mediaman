"""The dashboard and kept-items scripts update the page in place, never reload it."""

from __future__ import annotations

import json
import re
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

    def test_keep_that_empties_a_row_eases_the_grid_and_slides_the_rest_from_the_fade_start(self):
        out = _run("keepEmptiesRow")
        start = out["atFadeStart"]
        # one height animation, from three tiles' worth to two, before the fade ends
        assert start["grid"] == [["300px", "200px", 250]]
        # the tiles after the kept one (positions 1 and 2) already slide
        assert start["slid"] == [1, 2]
        assert start["pinned"] == "absolute"
        # the refresh that follows animates nothing more and leaves the grid clean
        assert out["heightAnims"] == 1
        assert out["tiles"] == ["b", "c"]
        assert out["overflow"] == ""

    def test_keep_with_reduced_motion_brings_the_next_button_into_view_at_once(self):
        out = _run("revealReduced")
        assert out["focused"]["id"] == "b"
        assert out["scrolls"] == [{"top": 148, "behavior": "instant"}]
        assert out["underTabs"] == [{"top": 12, "behavior": "instant"}]

    def test_refresh_landing_mid_slide_still_brings_the_focused_button_into_view(self):
        out = _run("revealAfterRefresh")
        assert out["focused"]["id"] == "b"
        assert out["scrolls"] == [{"top": 148, "behavior": "smooth"}]

    def test_scan_button_keeps_its_icon_through_every_state(self):
        seen = _run("scanLabel")["seen"]
        assert [s["label"] for s in seen] == ["Scanning", "Scan complete", "Scan now", "Retry scan"]
        assert all(s["icon"] for s in seen)
        assert seen[1]["icon"] == "#i-check"

    def test_failed_scan_is_told_beside_the_button_not_in_a_toast_too(self):
        out = _run("scanFailed")
        for case in (out["polled"], out["trigger"]):
            assert case["toasts"] == []
            assert case["status"]
            assert case["live"] == case["status"]
            assert case["label"] == "Retry scan"

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

    def test_watch_list_scripts_never_reload(self):
        for name in ("list.js", "admin.js"):
            src = (_JS / "watch" / name).read_text(encoding="utf-8")
            assert "location.reload" not in src, name

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


_CSS = _WEB / "static/css"


def _blocks(name: str, selector: str) -> list[str]:
    """Every declaration block whose selector list contains `selector`."""
    css = re.sub(r"/\*.*?\*/", "", (_CSS / name).read_text(encoding="utf-8"), flags=re.S)
    return [
        m.group(2)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [part.strip() for part in m.group(1).split(",")]
    ]


class TestScanStatusAndToastPlacement:
    def test_scan_status_takes_no_room_beside_the_button_so_it_cannot_move_it(self):
        pinned = _blocks("_buttons.css", ".u-flex:has(> .scan-btn) > .scan-status")
        assert any("position: absolute" in b for b in pinned)
        # emptying it must not take it out of layout, or it could not fade
        assert not any("display: none" in b for b in _blocks("_buttons.css", ".scan-status:empty"))

    def test_scan_status_fades_with_the_fast_duration(self):
        base = " ".join(_blocks("_buttons.css", ".scan-status"))
        assert "opacity var(--dur-fast)" in base

    def test_desktop_toasts_sit_at_the_bottom_clear_of_page_header_controls(self):
        css = (_CSS / "_ui_feedback.css").read_text(encoding="utf-8")
        desktop = css[css.index("@media (min-width: 700px)") :]
        desktop = desktop[: desktop.index("/* Mobile")]
        assert "top: auto" in desktop
        assert "bottom: var(--toast-bottom)" in desktop

    def test_tile_lift_only_where_a_pointer_can_hover(self):
        css = (_CSS / "_tiles.css").read_text(encoding="utf-8")
        assert re.search(r"@media \(hover: hover\) \{\s*\.tile:hover \{ transform", css)
        assert not re.search(r"^\.tile:hover \{", css, flags=re.M)


class TestGridAndAbandonDialogCss:
    def test_tile_grid_rows_keep_their_height_while_the_grid_height_animates(self):
        assert any("align-content: start" in b for b in _blocks("_tiles.css", ".grid-tiles"))

    def test_abandon_dialog_stacks_like_the_confirm_dialog_on_phones(self):
        css = re.sub(r"/\*.*?\*/", "", (_CSS / "_dl.css").read_text(encoding="utf-8"), flags=re.S)
        phone = css[css.index("@media (max-width: 699.98px)") :]
        assert re.search(r"\.abandon-modal-actions \{ flex-direction: column; \}", phone)
        assert re.search(
            r"\.abandon-modal-actions \.btn \{[^}]*width: 100%[^}]*min-height: 44px", phone
        )
        assert re.search(r"\.abandon-modal-body h2 \{ font-size: 18px; \}", phone)
