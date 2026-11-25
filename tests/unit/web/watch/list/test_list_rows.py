"""What a card of All titles carries for an admin's Keep and Delete.

A show's card stands for several seasons, so its Delete must never carry a deletable
item id; it carries the show key the season list is fetched with.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote

import pytest

from mediaman.web.repository.library_query import _query
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from tests.helpers.factories import (
    insert_kept_show,
    insert_media_item,
    insert_scheduled_action,
)

_AWKWARD_TEXT = """He said "Hi" & it's <b>bold</b>"""
_SEASON_IDS = {"s10", "s2", "s3"}


def _insert_show(conn, show_rating_key: str | None, show_title: str = "Breaking Bad") -> None:
    for media_id in _SEASON_IDS:
        insert_media_item(
            conn,
            id=media_id,
            title=f"{show_title} {media_id}",
            media_type="tv_season",
            show_title=show_title,
            season_number=int(media_id[1:]),
            plex_rating_key=f"rk-{media_id}",
            show_rating_key=show_rating_key,
            file_path=f"/media/tv/Breaking Bad/{media_id}",
            file_size_bytes=1_000,
        )


def _insert_movie(conn, media_id: str, title: str) -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        plex_rating_key=f"rk-{media_id}",
        file_path="/f",
        file_size_bytes=1_000,
    )


@pytest.mark.parametrize("show_rating_key", ["show-rk", None])
def test_a_show_s_delete_carries_no_season_id(admin_of, conn, show_rating_key):
    _insert_show(conn, show_rating_key)

    assert admin_of()["delete"] == {"show_rk": show_rating_key or "Breaking Bad"}


def test_a_show_s_delete_ignores_the_label_wording(admin_of, conn, monkeypatch):
    real = _query._shape_rows

    def reworded(*args, **kwargs):
        items = real(*args, **kwargs)
        for item in items:
            item["type_label"] = "1 season"
        return items

    monkeypatch.setattr(_query, "_shape_rows", reworded)
    _insert_show(conn, "show-rk")

    assert "id" not in admin_of()["delete"]


@pytest.mark.parametrize("type_", ["", "stale"])
def test_a_show_with_one_stale_season_of_three_still_carries_no_season_id(cards, conn, type_):
    """The filter is of the show: the one stale season must not stand for all of it."""
    _insert_show(conn, "show-rk")
    conn.execute("UPDATE media_items SET added_at = '2020-01-01T00:00:00+00:00' WHERE id = 's2'")

    (card,) = cards(f"type={type_}")

    assert (card["admin"]["seasons"], card["admin"]["delete"]) == (3, {"show_rk": "show-rk"})


def test_a_show_of_one_season_deletes_it_directly(admin_of, conn):
    insert_media_item(
        conn,
        id="solo",
        title="Solo S1",
        media_type="tv_season",
        show_title="Solo",
        season_number=1,
        plex_rating_key="rk-solo",
        show_rating_key="solo-rk",
        file_path="/media/tv/Solo",
        file_size_bytes=1_000,
    )

    delete = admin_of()["delete"]

    assert (delete["id"], delete["kind"], delete["show_rk"]) == ("solo", "season", "solo-rk")


@pytest.mark.parametrize("show_rating_key", ["show-rk", None])
def test_the_season_list_of_a_show_s_card_holds_every_grouped_season(
    client, admin_of, conn, show_rating_key
):
    _insert_show(conn, show_rating_key)

    resp = client.get(f"/api/show/{admin_of()['delete']['show_rk']}/seasons")

    assert resp.status_code == 200
    body = resp.json()
    assert {s["id"] for s in body["seasons"]} == _SEASON_IDS
    assert [s["season_number"] for s in body["seasons"]] == [2, 3, 10]
    assert body["show_title"] == "Breaking Bad"


def test_season_list_opens_for_a_title_with_a_slash(client, admin_of, conn):
    _insert_show(conn, None, show_title="Love/Hate")

    # Encoded as the page's script encodes it, with encodeURIComponent.
    resp = client.get(f"/api/show/{quote(admin_of()['delete']['show_rk'], safe='')}/seasons")

    assert resp.status_code == 200
    assert {s["id"] for s in resp.json()["seasons"]} == _SEASON_IDS


def test_season_list_leaves_out_another_shows_seasons(client, conn):
    _insert_show(conn, None)
    insert_media_item(
        conn,
        id="other",
        title="Other S1",
        media_type="tv_season",
        show_title="Other",
        season_number=1,
        plex_rating_key="rk-other",
        show_rating_key="other-rk",
        file_path="/media/tv/Other",
        file_size_bytes=1_000,
    )

    resp = client.get("/api/show/Breaking Bad/seasons")

    assert {s["id"] for s in resp.json()["seasons"]} == _SEASON_IDS


def test_movie_delete_carries_its_id_and_a_quoted_title(admin_of, conn):
    _insert_movie(conn, "m1", _AWKWARD_TEXT)

    admin = admin_of()

    assert (admin["delete"]["id"], admin["delete"]["kind"]) == ("m1", "movie")
    assert admin["title"] == _AWKWARD_TEXT


def test_remove_keep_carries_a_quoted_id(admin_of, conn):
    _insert_movie(conn, _AWKWARD_TEXT, "Dune")
    _keep(conn, _AWKWARD_TEXT)

    admin = admin_of()

    assert (admin["id"], admin["unkeep"]) == (_AWKWARD_TEXT, "item")


@pytest.mark.parametrize(("show_rating_key", "expected"), [("show-rk", True), (None, False)])
def test_a_show_flags_whether_its_key_is_a_rating_key(admin_of, conn, show_rating_key, expected):
    _insert_show(conn, show_rating_key)

    admin = admin_of()

    assert admin["has_rk"] is expected
    assert admin["show_rk"] == (show_rating_key or "Breaking Bad")


def _keep(conn, media_id: str) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="protected_forever",
        execute_at=None,
        token=f"tok-{media_id}",
    )


def test_a_partly_kept_show_keeps_its_delete_and_says_how_many_are_kept(admin_of, cards, conn):
    _insert_show(conn, "show-rk")
    assert admin_of()["has_keeps"] is False
    _keep(conn, "s3")

    admin = admin_of()

    assert (admin["kept"], admin["has_keeps"], admin["unkeep"]) == (False, True, None)
    assert "1 of 3 seasons kept" in admin["facts"]
    assert admin["delete"] == {"show_rk": "show-rk"}
    assert [c["subtitle"] for c in cards("type=kept")] == ["1 of 3 seasons kept"]


def test_remove_keep_of_a_fully_kept_show_opens_the_keep_dialog(admin_of, conn):
    _insert_show(conn, "show-rk")
    for media_id in _SEASON_IDS:
        _keep(conn, media_id)

    assert (admin_of()["unkeep"], admin_of()["delete"]) == ("dialog", None)


def test_remove_keep_of_a_single_season_show_kept_as_a_whole_opens_the_keep_dialog(admin_of, conn):
    insert_media_item(
        conn,
        id="s1",
        title="Show S01",
        media_type="tv_season",
        show_title="Show",
        season_number=1,
        plex_rating_key="rk-s1",
        show_rating_key="show-rk",
        file_path="/f",
        file_size_bytes=1_000,
    )
    insert_kept_show(conn, show_rating_key="show-rk", action="protected_forever")

    assert admin_of()["unkeep"] == "dialog"


@pytest.mark.parametrize(
    ("action", "execute_in", "token_used"),
    [
        ("protected_forever", None, True),
        ("snoozed", timedelta(hours=2), False),
        ("snoozed", timedelta(hours=2), True),
    ],
    ids=["forever-keep-from-email", "snooze-ending-today", "snooze-from-email-ending-today"],
)
def test_item_the_scanner_keeps_reads_kept_and_offers_no_delete(
    admin_of, conn, action, execute_in, token_used
):
    _insert_movie(conn, "m1", "Dune")
    insert_scheduled_action(
        conn,
        media_item_id="m1",
        action=action,
        execute_at=(datetime.now(UTC) + execute_in).isoformat() if execute_in else None,
        token="tok-m1",
        token_used=token_used,
    )

    admin = admin_of()

    assert (admin["kept"], admin["delete"]) == (True, None)


def test_a_scheduled_movie_says_its_deletion_date_and_days_left(cards, conn):
    _insert_movie(conn, "42", "Dune")
    insert_scheduled_action(
        conn,
        media_item_id="42",
        action="scheduled_deletion",
        # Noon of the fifth UTC day ahead: the count is calendar days, so the hour of the run must not matter.
        execute_at=datetime.combine(
            datetime.now(UTC).date() + timedelta(days=5), time(12), UTC
        ).isoformat(),
        token="tok-42",
    )

    (card,) = cards("sort=leaving")

    day = datetime.now(UTC).date() + timedelta(days=5)
    assert (card["subtitle"], card["tone"]) == (f"Leaves {day.day} {day:%b}", "soon")
    assert card["leaving_days"] == 5
    assert (card["admin"]["kept"], card["admin"]["deletion"]) == (False, True)


def test_a_movie_past_its_deletion_date_reads_any_day_and_is_counted_as_due(read, conn):
    _insert_movie(conn, "42", "Dune")
    insert_scheduled_action(
        conn,
        media_item_id="42",
        action="scheduled_deletion",
        execute_at=(datetime.now(UTC) - timedelta(days=3)).isoformat(),
        token="tok-42",
    )

    got = read("sort=leaving")

    assert got["items"][0]["subtitle"] == "Leaving any day"
    assert "1 leaving in the next 14 days" in got["fact"]


def test_a_kept_movie_reads_kept_where_the_date_would_be(cards, conn):
    _insert_movie(conn, "42", "Dune")
    _keep(conn, "42")

    (card,) = cards("sort=leaving")

    assert (card["subtitle"], card["tone"]) == ("Kept", None)
    assert (card["admin"]["kept"], card["admin"]["deletion"]) == (True, False)


@pytest.mark.parametrize("remove", [False, True])
def test_a_card_shown_before_another_admin_kept_its_film_forever_cannot_change_that_keep(
    client, admin_of, conn, remove
):
    until = datetime.now(UTC) + timedelta(days=20)
    _insert_movie(conn, "42", "Dune")
    insert_scheduled_action(
        conn, media_item_id="42", action="snoozed", execute_at=until.isoformat()
    )
    conn.commit()
    read = admin_of()["keep"]
    client.post("/api/media/42/keep", data={"duration": "forever"})

    if remove:
        resp = client.post("/api/media/42/unprotect", json={"read": read})
    else:
        form = {f"read_{key}": value or "" for key, value in read.items()}
        resp = client.post("/api/media/42/keep", data={"duration": "7 days", **form})

    assert (resp.status_code, resp.json()["error"]) == (409, "keep_changed")
    (row,) = conn.execute("SELECT action FROM scheduled_actions").fetchall()
    assert row["action"] == "protected_forever"


def test_a_title_opens_only_where_watch_holds_it_by_a_key_the_detail_accepts(cards, conn):
    _insert_movie(conn, "42", "Dune")
    _insert_movie(conn, "not-a-key", "Arrival")
    _insert_movie(conn, "43", "Elsewhere")
    conn.execute("UPDATE media_items SET plex_library_id = 9 WHERE id = '43'")
    _insert_show(conn, "77")

    hrefs = {c["title"]: c["href"] for c in cards()}

    assert hrefs == {
        "Dune": "/watch/movie/42",
        "Arrival": None,
        "Elsewhere": None,
        "Breaking Bad": "/watch/show/77",
    }


def test_each_title_of_the_library_has_the_admin_s_own_mark_to_toggle(cards, conn):
    for key in ("42", "43"):
        _insert_movie(conn, key, f"Film {key}")
    _insert_show(conn, "77")
    _insert_movie(conn, "not-a-key", "Arrival")
    admin = conn.execute("SELECT username FROM admin_users").fetchone()[0]
    mark_watched(conn, admin, [MarkTarget("42", "42", None, 0)], datetime(2026, 11, 23, tzinfo=UTC))

    marks = {(c["mark_kind"], c["mark_key"]): c["watched"] for c in cards() if c["mark_key"]}

    assert marks == {("movie", "42"): True, ("movie", "43"): False, ("show", "77"): False}
