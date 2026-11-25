"""What a show's page says of each season, and which of them the person may ask for."""

from __future__ import annotations

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.web.routes.watch.title_seasons import add_missing_seasons, fill_season_status
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
            **AIRED,
        },
    }


def _view(*seasons, answering=True):
    series = {"id": 9, "tmdbId": 77, "tvdbId": 5500, "monitored": True, "seasons": list(seasons)}
    return catalog.ArrView(
        {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {77: series} if seasons else {},
            "sonarr_queue_tmdb_ids": set(),
        },
        frozenset({"sonarr"} if answering else ()),
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
    page = _page(1, 2)
    page["seasons"][1]["episode_count"] = 0

    assert _fill(people, page, _view()) == {
        1: ("not_asked", False, True, None),
        2: ("not_out", False, True, None),
    }


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


def test_a_library_show_keeps_plexs_seasons_and_gains_those_only_tmdb_knows():
    plex = [{"number": 0, "key": "500"}, {"number": 2, "key": "502", "name": "Plex's two"}]
    tmdb = [{"number": n, "name": f"Season {n}", "episode_count": 8} for n in (1, 2, 3)]

    merged = add_missing_seasons(plex, tmdb)

    assert [(s["number"], s["key"]) for s in merged] == [
        (0, "500"),
        (1, None),
        (2, "502"),
        (3, None),
    ]
    assert merged[2] is plex[1]
    assert (merged[1]["in_library"], merged[1]["episode_count"]) == (False, 8)
