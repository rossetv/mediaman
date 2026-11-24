"""The storage meter, its panel and what they read are for admins: nothing of them reaches a viewer."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.db import set_connection
from mediaman.main import create_app
from tests.helpers.factories import insert_media_item, insert_scheduled_action, insert_settings

_CSS = Path(__file__).resolve().parents[4] / "src/mediaman/web/static/css/_watch_space.css"
_OF_IT = ("space-meter", "data-space-open", 'id="space"', "watch/space.js", "_watch_space.css")


@pytest.fixture
def app(conn, secret_key):
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(
        conn,
        id="100",
        title="Leaving Film",
        plex_rating_key="100",
        plex_library_id="1",
        file_path="/media/movies/secret-folder/film.mkv",
        file_size_bytes=5_000_000_000,
    )
    insert_scheduled_action(conn, media_item_id="100")
    conn.commit()
    set_connection(conn)
    app = create_app()
    app.state.config = SimpleNamespace(secret_key=secret_key)
    return app


def _page(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("path", ["/watch", "/watch?view=list", "/watch?q=film"])
def test_an_admin_s_page_has_the_meter_in_both_layouts_and_one_panel(
    app, authed_client, conn, path
):
    html = _page(authed_client(app, conn), path)

    assert all(part in html for part in _OF_IT)
    meters = re.findall(r'<button[^>]*class="space-meter[^"]*"', html)
    assert sorted("--phone" in m for m in meters) == [False, True]
    assert html.count('id="space"') == 1
    # out of the part of the page that is inert while it is open
    assert html.index('id="space"') > html.index("</main>")


def test_the_panel_s_tabs_and_panes_name_each_other_and_its_figures_come_later(
    app, authed_client, conn
):
    panel = _page(authed_client(app, conn), "/watch").split('id="space"')[1]

    for name in ("storage", "scheduled", "deleted"):
        assert f'id="space-tab-{name}"' in panel
        assert f'aria-controls="space-{name}"' in panel
        assert f'aria-labelledby="space-tab-{name}"' in panel
    assert "aria-modal" not in panel
    assert not re.search(r"\d+(\.\d+)? (KB|MB|GB|TB)", panel)


@pytest.mark.parametrize("path", ["/watch", "/watch?view=list", "/watch?q=film", "/downloads"])
def test_a_viewer_s_page_has_no_meter_panel_script_or_size(app, viewer_client, conn, path):
    html = _page(viewer_client(app, conn), path)

    for part in (*_OF_IT, "/api/dashboard", "secret-folder", "4.7 GB"):
        assert part not in html


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/dashboard/stats"),
        ("GET", "/api/dashboard/scheduled"),
        ("GET", "/api/dashboard/deleted"),
        ("GET", "/api/dashboard/reclaimed-chart"),
        ("GET", "/api/library?type=movie&sort=size_desc&per_page=8"),
        ("GET", "/api/scan/status"),
        ("POST", "/api/scan/trigger"),
        ("POST", "/api/scan/clear-scheduled"),
        ("POST", "/api/media/100/keep"),
        ("POST", "/api/media/redownload"),
    ],
)
def test_what_the_panel_reads_and_does_is_refused_to_a_viewer(
    app, viewer_client, conn, method, path
):
    resp = viewer_client(app, conn).request(
        method, path, headers={"Origin": "http://testserver"}, follow_redirects=False
    )

    assert resp.status_code == 403
    assert "Leaving Film" not in resp.text


def _rules(selector: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    return " ".join(re.findall(rf"(?:^|[}}{{])\s*{re.escape(selector)} \{{([^}}]*)\}}", css))


def test_the_phone_s_meter_gives_way_to_the_profile_button_beside_it():
    meter = _rules(".space-meter--phone")
    words = _rules(".space-meter--phone .space-meter__free")

    assert "flex: 0 1 auto" in meter
    assert "min-width: 44px" in meter
    assert "min-width: 0" in words
    assert "text-overflow: ellipsis" in words


def test_a_narrow_phone_drops_the_bar_and_the_drive_for_the_marks_before_it_cuts_the_words():
    any_mark = ":has(> :is(.space-meter__warn, .space-meter__info))"
    both = ":has(> .space-meter__warn):has(> .space-meter__info)"

    for gone in (
        f".space-meter--phone{any_mark} > .space-meter__bar",
        f".space-meter--off{any_mark} > .ico:first-child",
        f".space-meter--phone{both} > .ico:first-child",
    ):
        assert "display: none" in _rules(gone), gone


def test_a_figure_too_long_for_a_phone_wraps():
    assert "flex-wrap: wrap" in _rules(".space-fig__n")


def test_a_scheduled_title_s_tap_area_is_its_whole_text_on_a_phone():
    assert "inset: -6px 0" in _rules("a.space-item__title::after")
    assert "position: relative" in _rules(".space-item__text:has(> a)")


def test_the_narrowest_phone_keeps_the_bar_beside_the_words_of_a_disk_that_was_read():
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    hiding = re.findall(r"([^{}]*)\{[^{}]*display: none", css)
    hidden = [sel.strip() for rule in hiding for sel in rule.split(",") if "meter__bar" in sel]

    assert hidden
    assert all("__warn" in sel or "__info" in sel for sel in hidden)
