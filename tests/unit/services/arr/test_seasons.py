"""The state of each season of a show, and adding one."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.services.arr.base import SearchUnanswered
from mediaman.services.arr.seasons import add_season, queued_seasons, season_states
from mediaman.services.infra import SafeHTTPError

AIRED = "2020-01-01T00:00:00Z"
NEXT = "2099-01-01T00:00:00Z"


def _season(number, files=0, wanted=0, total=0, monitored=False, **stats):
    counts = {"episodeFileCount": files, "episodeCount": wanted, "totalEpisodeCount": total}
    return {"seasonNumber": number, "monitored": monitored, "statistics": {**counts, **stats}}


def _state(season, *, monitored=True, queue=None):
    series = {"id": 9, "monitored": monitored, "seasons": [season, _season(99, total=1)]}
    found = season_states(series, queue or {})[season["seasonNumber"]]
    return found.status, found.asked


@pytest.mark.parametrize(
    ("season", "expected"),
    [
        (_season(1, 8, 8, 8, previousAiring=AIRED), ("held", False)),
        # An airing season with all that has aired so far.
        (_season(1, 3, 3, 8, True, previousAiring=AIRED, nextAiring=NEXT), ("held", True)),
        (_season(1, 3, 3, 8, previousAiring=AIRED), ("partly_held", False)),
        (_season(1, 3, 8, 8, True, previousAiring=AIRED), ("partly_held", True)),
        (_season(1, 0, 8, 8, True, previousAiring=AIRED), ("asked", True)),
        (_season(1, 0, 0, 8, previousAiring=AIRED), ("not_asked", False)),
        (_season(1, 0, 0, 8, nextAiring=NEXT), ("not_out", False)),
        (_season(1, 0, 0, 8, True, nextAiring=NEXT), ("not_out", True)),
        (_season(1, 0, 0, 0), ("not_out", False)),
    ],
)
def test_a_season_is_what_sonarr_holds_wants_and_has_seen_air(season, expected):
    assert _state(season) == expected


def test_nothing_of_an_unmonitored_series_is_asked():
    season = _season(1, 0, 8, 8, True, previousAiring=AIRED)

    assert _state(season, monitored=False) == ("not_asked", False)


def test_a_show_just_added_whose_episodes_are_not_counted_is_not_called_unaired():
    series = {"id": 9, "monitored": True, "seasons": [{"seasonNumber": 1, "monitored": True}]}

    assert season_states(series, {})[1].status == "asked"


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


_REFUSED = SafeHTTPError(500, "no", "sonarr/command")


def _sonarr(error, after):
    client = MagicMock()
    client.remonitor_season.side_effect = error
    client.get_series_by_id.return_value = {"monitored": True, "seasons": [after]}
    return client


@pytest.mark.parametrize(
    ("error", "monitored_after", "added"),
    [
        (SearchUnanswered("no answer"), False, True),
        (_REFUSED, True, True),
        (_REFUSED, False, False),
    ],
)
def test_a_season_is_added_once_sonarr_wants_it_whatever_became_of_the_search(
    error, monitored_after, added
):
    client = _sonarr(error, {"seasonNumber": 3, "monitored": monitored_after})

    assert add_season(client, 9, 3) is added
    client.unmonitor_season.assert_not_called()
