"""What a show's page says of each season, and which of them the person may ask for."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.web.routes.watch.title_seasons import (
    begin_arr_view,
    fill_season_status,
    missing_seasons,
)
from tests.helpers.factories import (
    insert_admin_user,
    insert_audit_log,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)

AIRED = {"previousAiring": "2020-01-01T00:00:00Z"}


def _sonarr_season(number, files=0, total=8, monitored=False):
    return {
        "seasonNumber": number,
        "monitored": monitored,
        "statistics": {
            "episodeFileCount": files,
            "episodeCount": total if monitored else files,
            "totalEpisodeCount": total,
            # Sonarr gives no airing dates of what is not monitored.
            **(AIRED if monitored else {}),
        },
    }


def _view(*seasons, answering=True, monitored=True, odd=()):
    ids = {"id": 9, "tmdbId": 77, "tvdbId": 5500}
    series = {**ids, "monitored": monitored, "seasons": list(seasons)}
    return catalog.ArrView(
        {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {77: series} if seasons else {},
            "sonarr_queue_tmdb_ids": set(),
        },
        frozenset({"sonarr"} if answering else ()),
        odd_shows=frozenset(odd),
    )


def _page(*numbers, held=()):
    return {
        "title": "Glass Coast",
        "seasons": [
            {
                "number": n,
                "episode_count": 8,
                "in_library": n in held,
                "key": f"50{n}" if n in held else None,
            }
            for n in numbers
        ],
        "more_seasons": [],
        "seasons_request": None,
    }


@pytest.fixture
def people(conn):
    insert_admin_user(conn, username="amy", role="viewer")
    insert_admin_user(conn, username="boss", role="admin")
    insert_settings(conn, plex_libraries='["1"]')
    return conn


def _fill(conn, page, view, username="amy", **ids):
    fill_season_status(page, conn, view, username, **{"tmdb_id": 77, "tvdb_id": 5500, **ids})
    return {
        s["number"]: (s["status"], s["asked"], s["askable"], s["refusal"]) for s in page["seasons"]
    }


def _delete(conn, season):
    audit = insert_audit_log(conn, media_item_id="502", action="deleted")
    conn.execute(
        "INSERT INTO deleted_media (audit_id, media_type, tvdb_id, season_number, title)"
        " VALUES (?, 'tv_season', 5500, ?, 'Glass Coast')",
        (audit, season),
    )
    conn.commit()


def test_a_tracked_show_says_what_can_be_asked_for(people):
    view = _view(
        _sonarr_season(1, files=8),
        _sonarr_season(2, files=3),
        _sonarr_season(3, monitored=True),
        _sonarr_season(4),
    )

    assert _fill(people, _page(1, 2, 3, 4, 5), view) == {
        1: ("held", False, False, None),
        2: ("partly_held", False, True, None),
        3: ("asked", True, False, None),
        4: ("not_asked", False, True, None),
        5: ("not_asked", False, False, "not_listed"),
    }


def test_a_show_sonarr_does_not_track_can_be_asked_for_all_of(people):
    page = _page(1, 2, 3)
    page["seasons"][1]["episode_count"] = 0
    tmdb = [{"number": 1, "air_date": "2020-01-01"}, {"number": 3, "air_date": "2099-01-01"}]

    assert _fill(people, page, _view(), tmdb_seasons=tmdb) == {
        1: ("not_asked", False, True, None),
        2: ("not_out", False, True, None),
        3: ("not_out", False, True, None),
    }


def test_an_unmonitored_season_is_out_or_not_as_tmdb_dates_it(people):
    tmdb = [{"number": 1, "air_date": "2020-01-01"}, {"number": 2, "air_date": "2099-01-01"}]

    found = _fill(
        people, _page(1, 2), _view(_sonarr_season(1), _sonarr_season(2)), tmdb_seasons=tmdb
    )

    assert [found[1][0], found[2][0]] == ["not_asked", "not_out"]


@pytest.mark.parametrize(
    ("username", "deleted", "refusal"),
    [
        ("amy", None, "show_unmonitored"),
        ("boss", None, None),
        # It would be fetched again.
        ("boss", 1, "show_unmonitored"),
    ],
)
def test_a_season_that_would_start_others_of_a_show_that_is_off_is_not_a_viewers_to_ask_for(
    people, username, deleted, refusal
):
    if deleted:
        _delete(people, deleted)
    view = _view(_sonarr_season(1, monitored=True), _sonarr_season(2), monitored=False)

    assert _fill(people, _page(2), view, username)[2] == (
        "not_asked",
        False,
        refusal is None,
        refusal,
    )


@pytest.mark.parametrize(
    ("username", "monitored", "starts"),
    [("boss", False, [1, 3]), ("boss", True, None), ("amy", False, None)],
)
def test_an_admin_alone_is_told_which_seasons_asking_would_start(
    people, username, monitored, starts
):
    seasons = [_sonarr_season(1, monitored=True), _sonarr_season(2)]
    page = _page(1, 2)

    view = _view(*seasons, _sonarr_season(3, monitored=True), monitored=monitored)
    _fill(people, page, view, username)

    assert page.get("also_starts") == starts


def test_a_season_sonarr_gave_in_a_shape_that_cannot_be_read_has_no_status(people):
    found = _fill(people, _page(1, 2), _view(_sonarr_season(1), odd=[77]))

    assert found == {1: ("not_asked", False, True, None), 2: (None, False, False, None)}


def test_without_an_answer_from_sonarr_nothing_is_claimed_or_offered(people):
    assert _fill(people, _page(1, 2, held=[1]), _view(answering=False)) == {
        1: ("held", False, False, None),
        2: (None, False, False, None),
    }


@pytest.mark.parametrize(
    ("column", "value", "status"),
    [("unlisted", 0, "held"), ("unlisted", 1, "not_asked"), ("plex_library_id", 9, "not_asked")],
)
def test_a_season_the_last_scan_listed_is_held_though_sonarr_has_nothing_of_it(
    people, column, value, status
):
    insert_media_item(
        people, id="502", media_type="tv_season", season_number=2, show_title="Glass Coast"
    )
    people.execute(f"UPDATE media_items SET tvdb_id = 5500, {column} = ?", (value,))

    assert _fill(people, _page(2), _view(_sonarr_season(2)))[2][0] == status


@pytest.mark.parametrize(
    ("username", "askable", "refusal"), [("amy", False, "removed"), ("boss", True, None)]
)
def test_a_season_mediaman_deleted_is_offered_to_an_admin_alone(people, username, askable, refusal):
    _delete(people, 2)

    found = _fill(people, _page(2, 3), _view(_sonarr_season(2), _sonarr_season(3)), username)

    assert found[2] == ("not_asked", False, askable, refusal)
    assert found[3] == ("not_asked", False, True, None)


def test_a_part_of_a_season_that_is_leaving_is_offered_to_nobody(people):
    insert_media_item(
        people, id="502", media_type="tv_season", season_number=2, show_title="Glass Coast"
    )
    people.execute("UPDATE media_items SET tvdb_id = 5500")
    insert_scheduled_action(people, media_item_id="502", action="scheduled_deletion")

    found = _fill(people, _page(2), _view(_sonarr_season(2, files=3)), "boss")

    assert found[2] == ("partly_held", False, False, "leaving")


def test_what_asks_for_seasons_is_the_picks_own_route_or_searchs(people):
    pick, show = _page(1), _page(1)
    _fill(people, pick, _view(), download_url="/api/watch/picks/7/download")
    _fill(people, show, _view(_sonarr_season(1)), tmdb_id=None)

    assert pick["seasons_request"] == {"url": "/api/watch/picks/7/download", "body": {}}
    assert show["seasons_request"] == {
        "url": "/api/search/download",
        "body": {"media_type": "tv", "tmdb_id": 77, "title": "Glass Coast"},
    }


def test_the_seasons_only_tmdb_knows_of_come_beside_plexs_and_have_a_status_too(people):
    page = _page(0, 2, held=(0, 2))
    tmdb = [{"number": n, "name": f"Season {n}", "episode_count": 8} for n in (1, 2, 3)]
    page["more_seasons"] = missing_seasons(page["seasons"], tmdb)

    _fill(people, page, _view(_sonarr_season(1), _sonarr_season(2), _sonarr_season(3)))

    assert [s["number"] for s in page["seasons"]] == [0, 2]
    assert [
        (s["number"], s["key"], s["in_library"], s["askable"]) for s in page["more_seasons"]
    ] == [
        (1, None, False, True),
        (3, None, False, True),
    ]


def test_a_page_says_when_the_sonarr_view_is_still_to_come(people):
    ready, early = _page(1), _page(1)
    _fill(people, ready, replace(_view(answering=False), generation=0))
    _fill(people, early, _view(answering=False))

    assert (ready["seasons_pending"], early["seasons_pending"]) == (False, True)


@pytest.mark.parametrize("sonarr_set_up", [False, True])
def test_a_view_that_comes_late_still_offers_no_way_to_ask_without_sonarr(
    people, secret_key, monkeypatch, sonarr_set_up
):
    if sonarr_set_up:
        insert_settings(people, sonarr_url="http://sonarr.test", sonarr_api_key="k")
    monkeypatch.setattr(catalog, "arr_view", lambda *_a, **_k: _view(answering=False))
    page = _page(1)

    _fill(people, page, begin_arr_view(people, secret_key)())

    assert (page["seasons_request"] is not None) is sonarr_set_up
    assert page["seasons"][0]["askable"] is False
