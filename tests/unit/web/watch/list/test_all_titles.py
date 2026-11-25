"""GET /api/watch/library/library/row, All titles: its filters, orders, bar, and whose they are."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import library_query
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes.watch import all_titles, library_row_api, pages
from tests.helpers.factories import (
    insert_admin_user,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)
from tests.unit.web.watch.list.conftest import ROW, tick_library

NOW = datetime.now(UTC)
_ADMIN_SORTS = ("size", "small", "seen", "stalest", "leaving")


def _movie(conn, key, title, *, days=1, size=1_000, watched=None, library=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        added_at=NOW - timedelta(days=days),
        file_size_bytes=size,
        plex_library_id=library,
    )
    if watched is not None:
        conn.execute(
            "UPDATE media_items SET last_watched_at = ? WHERE id = ?",
            ((NOW - timedelta(days=watched)).isoformat(), key),
        )


def _season(conn, key, show, title, kind="tv_season", number=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type=kind,
        season_number=number,
        show_rating_key=show,
        plex_library_id=1,
        added_at=NOW - timedelta(days=200),
    )


@pytest.fixture
def titled(conn):
    _movie(conn, "101", "The Amber Garden", days=1, size=5_000_000_000, watched=3)
    _movie(conn, "102", "Brightwater", days=90, size=9_000_000_000)
    _movie(conn, "103", "Copperline", days=2, size=1_000_000_000, watched=40)
    _season(conn, "501", "500", "Dunmarrow")
    _season(conn, "601", "600", "Embers", "anime_season")
    _movie(conn, "900", "Forgotten Reel", days=3, size=2_000_000_000, library=9)
    until = (NOW + timedelta(days=9)).isoformat()
    insert_scheduled_action(
        conn, media_item_id="103", action="scheduled_deletion", execute_at=until
    )
    insert_scheduled_action(
        conn, media_item_id="102", action="protected_forever", execute_at=None, token="t2"
    )
    conn.commit()


@pytest.fixture
def viewer(app_factory, conn):
    tick_library(conn)
    insert_admin_user(conn, username="ivo", role="viewer")
    client = TestClient(app_factory(library_row_api.router, pages.router, conn=conn))
    client.cookies.set("session_token", create_session(conn, "ivo"))
    return client


def _names(items):
    return [c["title"] for c in items]


@pytest.mark.parametrize(
    ("type_", "names"),
    [
        ("movies", ["The Amber Garden", "Copperline", "Forgotten Reel", "Brightwater"]),
        ("tv", ["Dunmarrow"]),
        ("anime", ["Embers"]),
        ("kept", ["Brightwater"]),
        ("stale", ["Brightwater", "Dunmarrow", "Embers"]),
    ],
)
def test_each_filter_has_its_titles_and_the_bar_counts_them(titled, read, type_, names):
    got = read(f"type={type_}")

    assert _names(got["items"]) == names
    assert (got["total"], got["counts"][type_]) == (len(names), len(names))
    assert got["counts"][""] == 6


def test_not_watched_goes_by_the_person_s_own_marks(titled, read, conn):
    admin = conn.execute("SELECT username FROM admin_users").fetchone()[0]
    mark_watched(conn, admin, [MarkTarget("101", "101", None, 0)], NOW)

    got = read("type=unseen&sort=az")

    # What anyone watched says nothing of it.
    assert "The Amber Garden" not in _names(got["items"])
    assert "Copperline" in _names(got["items"])
    assert got["counts"]["unseen"] == 5


@pytest.mark.parametrize(
    ("sort", "names"),
    [
        ("size", ["Brightwater", "The Amber Garden", "Forgotten Reel", "Copperline"]),
        ("small", ["Copperline", "Forgotten Reel", "The Amber Garden", "Brightwater"]),
        ("seen", ["The Amber Garden", "Copperline", "Brightwater", "Forgotten Reel"]),
        ("stalest", ["Brightwater", "Forgotten Reel", "Copperline", "The Amber Garden"]),
        ("leaving", ["Copperline", "The Amber Garden", "Brightwater", "Forgotten Reel"]),
        ("az", ["The Amber Garden", "Brightwater", "Copperline", "Forgotten Reel"]),
    ],
)
def test_every_order_is_whole_and_the_same_over_pages(titled, read, sort, names):
    assert _names(read(f"type=movies&sort={sort}")["items"]) == names
    paged, after = [], ""
    while after is not None:
        got = read(f"type=movies&sort={sort}&limit=1&after={after}")
        paged += _names(got["items"])
        after = got["next"]
    assert paged == names


def test_the_line_under_a_poster_says_what_the_order_goes_by(titled, cards):
    def said(sort):
        return {c["title"]: (c["subtitle"], c["tone"]) for c in cards(f"type=movies&sort={sort}")}

    assert said("size")["Brightwater"] == ("8.4 GB · Kept", None)
    assert said("stalest")["Brightwater"] == ("Never watched", "bad")
    assert said("seen")["The Amber Garden"] == ("Watched 3 days ago", None)
    assert said("leaving")["Brightwater"] == ("Kept", None)
    assert said("leaving")["The Amber Garden"] == ("Not scheduled", None)
    assert said("added")["The Amber Garden"] == ("Movie · 2024", None)


def test_the_bar_says_the_disk_and_what_leaves_of_the_filter(titled, read):
    assert read("type=movies")["fact"] == "15.8 GB on disk · 1 leaving in the next 14 days"
    assert read("type=kept")["fact"] == "8.4 GB on disk · 0 leaving in the next 14 days"
    assert read("type=stale")["fact"].endswith(" · 30+ days, unwatched")


def test_a_title_only_a_scan_knows_is_an_admin_s_with_what_can_be_done_of_it(titled, cards):
    (reel,) = [c for c in cards() if c["title"] == "Forgotten Reel"]

    assert (reel["href"], reel["mark_key"]) == (None, None)
    assert reel["admin"]["delete"]["id"] == "900"


def test_one_title_is_read_alone_with_the_bar_though_the_filter_does_not_hold_it(titled, read):
    got = read("type=kept&only=id:101")
    assert (_names(got["items"]), got["total"]) == (["The Amber Garden"], 1)
    assert read("only=id:gone")["items"] == []


def test_a_viewer_gets_what_watch_holds_and_nothing_of_an_admin_s(titled, viewer):
    got = viewer.get(ROW).json()

    assert "Forgotten Reel" not in _names(got["items"])
    assert (got["fact"], got["total"]) == (None, 5)
    assert set(got["counts"]) == {"", "movies", "tv", "anime", "unseen"}
    for card in got["items"]:
        assert not {"admin", "tone"} & set(card)
        assert "GB" not in card["subtitle"]


@pytest.mark.parametrize(
    "query",
    [*(f"sort={s}" for s in _ADMIN_SORTS), "type=kept", "type=stale", "type=kept&only=id:102"],
)
def test_a_viewer_is_refused_an_admin_s_filters_and_orders(titled, viewer, query):
    assert viewer.get(f"{ROW}?{query}").status_code == 403


def test_a_viewer_s_link_to_an_admin_s_filter_and_order_shows_the_usual_one(
    titled, viewer, monkeypatch, app_factory, conn, templates_stub
):
    monkeypatch.setattr(pages, "plex_is_configured", lambda _conn: True)
    app = app_factory(pages.router, conn=conn, state_extras={"templates": templates_stub})
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "ivo"))

    ctx = client.get("/watch?all=library&type=stale&sort=size").json()

    assert (ctx["grid"]["type"], ctx["grid"]["sort"], ctx["grid"]["fact"]) == ("", "added", None)
    assert [t[0] for t in ctx["library_types"]] == ["", "movies", "tv", "anime", "unseen"]
    assert all("admin" not in card for card in ctx["grid"]["items"])


@pytest.mark.parametrize("kind", ["movies", "tv", "anime", "recent"])
@pytest.mark.parametrize("sort", _ADMIN_SORTS)
def test_a_row_has_none_of_an_admin_s_orders(client, kind, sort):
    resp = client.get(f"/api/watch/library/{kind}/row?sort={sort}")
    assert resp.status_code == 400


@pytest.mark.parametrize("query", ["type=films", "after=nonsense"])
def test_a_filter_or_place_that_is_none_is_refused(client, query):
    assert client.get(f"{ROW}?{query}").status_code == 400


def test_signed_out_it_is_refused(app_factory, conn):
    assert TestClient(app_factory(library_row_api.router, conn=conn)).get(ROW).status_code == 401


def test_an_empty_library_says_what_comes_next(read, conn):
    assert read()["empty"]["title"] == "Connect Plex to get started"
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    assert read()["empty"]["href"] == "/watch#storage"
    assert read("type=kept")["empty"] == {
        "title": "Nothing is kept",
        "link": "Show all titles",
        "href": "/watch?all=library",
        "filter": "",
    }


def test_a_card_is_given_the_keep_it_shows_though_another_is_made_as_it_is_read(
    cards, conn, db_path, monkeypatch
):
    until = (NOW + timedelta(days=20)).isoformat()
    _movie(conn, "42", "Dune")
    insert_scheduled_action(conn, media_item_id="42", action="snoozed", execute_at=until)
    conn.commit()
    shape = library_query.shape_display_rows

    def then_kept_forever(*args):
        found = shape(*args)
        with sqlite3.connect(db_path) as other:
            other.execute("UPDATE scheduled_actions SET action = 'protected_forever'")
        return found

    monkeypatch.setattr(all_titles, "shape_display_rows", then_kept_forever)

    (card,) = cards()

    assert card["admin"]["kept"] is True
    assert card["admin"]["keep"]["action"] == "snoozed"
