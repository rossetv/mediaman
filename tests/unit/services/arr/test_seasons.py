"""The state of each season of a show."""

from __future__ import annotations

import pytest

from mediaman.services.arr.seasons import queued_seasons, readable_queue, show_seasons

AIRED = "2020-01-01T00:00:00Z"
NEXT = "2099-01-01T00:00:00Z"


def _season(number, files=0, wanted=0, total=0, monitored=False, **stats):
    counts = {"episodeFileCount": files, "episodeCount": wanted, "totalEpisodeCount": total}
    return {"seasonNumber": number, "monitored": monitored, "statistics": {**counts, **stats}}


def _state(season, *, monitored=True, queue=None, airs=None):
    series = {"id": 9, "monitored": monitored, "seasons": [season, _season(99, total=1)]}
    number = season["seasonNumber"]
    found = show_seasons(series, queue or {}, {number: airs}).states[number]
    return found.status, found.asked


# Sonarr gives no airing dates of what is not monitored.
@pytest.mark.parametrize(
    ("season", "airs", "expected"),
    [
        (_season(1, 8, 8, 8), None, ("held", False)),
        # An airing season with all that has aired so far.
        (_season(1, 3, 3, 8, True, previousAiring=AIRED, nextAiring=NEXT), None, ("held", True)),
        (_season(1, 3, 3, 8), None, ("partly_held", False)),
        (_season(1, 3, 8, 8, True, previousAiring=AIRED), None, ("partly_held", True)),
        (_season(1, 0, 8, 8, True, previousAiring=AIRED), None, ("asked", True)),
        (_season(1, 0, 0, 8), "2020-01-01", ("not_asked", False)),
        (_season(1, 0, 0, 8), "2099-01-01", ("not_out", False)),
        (_season(1, 0, 0, 8), None, ("not_asked", False)),
        (_season(1, 0, 0, 8), "soon", ("not_asked", False)),
        (_season(1, 0, 0, 8, True, nextAiring=NEXT), None, ("not_out", True)),
        # One whose aired episodes were unmonitored one by one.
        (_season(1, 0, 0, 8, True, nextAiring=NEXT), "2020-01-01", ("asked", True)),
        (_season(1, 0, 0, 0), None, ("not_out", False)),
    ],
)
def test_a_season_is_what_sonarr_holds_and_wants_and_what_has_aired(season, airs, expected):
    assert _state(season, airs=airs) == expected


def test_nothing_of_an_unmonitored_series_is_asked_and_adding_to_it_starts_its_other_seasons():
    season = _season(1, 0, 8, 8, True, previousAiring=AIRED)
    off = {"id": 9, "monitored": False, "seasons": [season, _season(2, total=8)]}

    assert _state(season, monitored=False) == ("not_asked", False)
    assert show_seasons(off, {}, {}).started_with(2) == {1}
    assert show_seasons({**off, "monitored": True}, {}, {}).started_with(2) == set()


def test_a_show_just_added_whose_episodes_are_not_counted_is_not_called_unaired():
    series = {"id": 9, "monitored": True, "seasons": [{"seasonNumber": 1, "monitored": True}]}

    assert show_seasons(series, {}, {}).states[1].status == "asked"


@pytest.mark.parametrize(
    "odd",
    [
        None,
        "x",
        {"seasonNumber": None},
        {"seasonNumber": 2, "statistics": "x"},
        {"seasonNumber": 2, "statistics": {"episodeCount": None}},
    ],
)
def test_a_season_that_cannot_be_read_is_skipped_and_said_to_be(odd):
    found = show_seasons({"id": [], "seasons": [odd, _season(1, 8, 8, 8)]}, {}, {})

    assert (list(found.states), found.whole) == ([1], False)
    assert show_seasons({"seasons": "x"}, {}, {}).whole is False


def _entry(season, status="downloading", series_id=9):
    return {
        "series": {"id": series_id},
        "episode": {"seasonNumber": season},
        "status": status,
        "size": 100,
        "sizeleft": 100 if status == "queued" else 50,
    }


def test_the_queue_is_told_by_season_and_a_failed_entry_brings_nothing():
    queue = queued_seasons(
        [
            _entry(1, "queued"),
            _entry(1),
            _entry(1, "queued"),
            _entry(2, "queued"),
            _entry(3, "failed"),
        ]
    )

    assert queue == {(9, 1): "downloading", (9, 2): "queued"}


def test_a_season_coming_in_says_so_unless_it_is_all_there():
    queue = queued_seasons([_entry(1)])

    assert _state(_season(1, 3, 8, 8, True), queue=queue)[0] == "downloading"
    assert _state(_season(1, 8, 8, 8, True), queue=queue)[0] == "held"
    # Another show's download is not this one's.
    assert _state(_season(1, 3, 8, 8, True), queue=queued_seasons([_entry(1, series_id=8)]))[0] == (
        "partly_held"
    )


def test_queue_entries_that_cannot_be_read_are_skipped():
    odd = [{"series": "x"}, {**_entry(1), "episode": [1]}, {**_entry(1), "size": "big"}]

    assert queued_seasons(readable_queue([*odd, _entry(2)])) == {(9, 2): "downloading"}
