"""The short answer about what is downloading: its states, what stays of a service that does not
answer, and one build shared by everybody."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services.downloads import strip
from mediaman.services.downloads.download_format import format_eta
from mediaman.services.downloads.download_queue import VIEWER_OUTAGE
from mediaman.services.downloads.strip import Answer, Shared, Strip, StripItem, collect, payload

_NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


def _raw(dl_id: str = "radarr:Ember Coast", **over: object) -> dict[str, object]:
    return {
        "id": dl_id,
        "title": dl_id.partition(":")[2] or dl_id,
        "media_type": "movie",
        "poster_url": "",
        "state": "downloading",
        "progress": 40,
        "eta": "",
        "search_hint": "",
        "search_count": 0,
        "arr_source": "",
    } | over


def _data(*queue: dict[str, object], recent=(), unreachable=(), hero=None):
    return {
        "hero": hero,
        "queue": list(queue),
        "upcoming": [],
        "recent": list(recent),
        "subtitle": "",
        "unreachable": list(unreachable),
        "outage": "",
    }


def _done(dl_id: str, seconds_ago: float, **over: object) -> dict[str, object]:
    return {
        "id": dl_id,
        "title": "Ember Coast",
        "media_type": "movie",
        "poster_url": "",
        "readiness": "ready",
        "completed_at": f"{_NOW - timedelta(seconds=seconds_ago):%Y-%m-%d %H:%M:%S}",
    } | over


def _one(raw: dict[str, object]) -> StripItem:
    [item] = collect(_data(raw), (), _NOW)
    return item


@pytest.mark.parametrize(
    ("over", "state"),
    [
        ({"state": "downloading"}, "downloading"),
        ({"state": "almost_ready"}, "almost_ready"),
        # NZBGet holds it and has not begun.
        ({"state": "searching", "arr_source": ""}, "queued"),
        ({"state": "searching", "arr_source": "Radarr", "search_count": 8}, "searching"),
        ({"state": "searching", "arr_source": "Radarr", "search_count": 9}, "stuck"),
    ],
)
def test_each_download_is_in_the_state_the_row_shows(over, state):
    assert _one(_raw(**over)).state == state


def test_a_stuck_one_says_when_it_is_searched_for_again():
    stuck = _raw(
        state="searching",
        arr_source="Radarr",
        search_count=12,
        search_hint="Searched 12× · next attempt in ~4h",
    )
    assert _one(stuck).line == "next attempt in ~4h"


@pytest.mark.parametrize(
    ("remain_mb", "minutes"), [(0.5, 1), (14 * 60, 14), (65 * 60, 65), (0, None)]
)
def test_the_time_left_is_the_one_the_downloads_page_words(remain_mb, minutes):
    assert _one(_raw(eta=format_eta(remain_mb, 1024 * 1024))).minutes_left == minutes


def test_the_item_on_top_of_the_downloads_page_is_in_the_row_too():
    items = collect(_data(_raw("radarr:B"), hero=_raw("radarr:A")), (), _NOW)
    assert [i.key for i in items] == ["radarr:A", "radarr:B"]


def test_two_films_of_one_title_are_two_items_with_no_id_known():
    items = collect(_data(_raw(), _raw()), (), _NOW)
    assert len({i.key for i in items}) == 2


@pytest.mark.parametrize("ids", [(7, 8), (8, 7)])
def test_two_films_of_one_title_keep_their_keys_in_whichever_order(ids):
    first = {i.tmdb_id: i.key for i in collect(_data(_raw(tmdb_id=7), _raw(tmdb_id=8)), (), _NOW)}
    raws = [_raw(tmdb_id=i) for i in ids]
    assert {i.tmdb_id: i.key for i in collect(_data(*raws), (), _NOW)} == first


def test_of_two_films_of_one_title_gone_at_once_none_is_guessed_to_be_the_one_done():
    before = collect(_data(_raw(tmdb_id=7), _raw(tmdb_id=8)), (), _NOW)
    [ready] = collect(_data(recent=[_done("radarr:Ember Coast", 1)]), before, _NOW)
    assert ready.tmdb_id is None
    still = [_done("radarr:Ember Coast", 1)]
    left = collect(_data(_raw(tmdb_id=8), recent=still), before, _NOW)
    assert {i.tmdb_id: i.state for i in left} == {7: "ready", 8: "downloading"}
    assert [i.tmdb_id for i in collect(_data(recent=still), left, _NOW)] == [7]


def test_a_title_whose_id_becomes_known_is_not_held_beside_itself():
    before = collect(_data(_raw()), (), _NOW)
    items = collect(_data(_raw(tmdb_id=7), unreachable=["Radarr"]), before, _NOW)
    assert [i.tmdb_id for i in items] == [7]


_RELEASE = "a8f1c2d9e07b44-[IndexerTag]"


def test_a_release_only_nzbget_holds_is_told_by_neither_its_name_nor_its_title():
    items = collect(_data(_raw(_RELEASE, title=_RELEASE)), (), _NOW)
    told = str(payload(Answer(Strip(tuple(items)), 0.0)))
    assert "a8f1c2d9e07b44" not in told
    assert "IndexerTag" not in told
    assert items[0].title == strip.UNNAMED


def test_a_release_with_a_readable_name_keeps_its_title():
    [item] = collect(_data(_raw("Ember.Coast.2024.1080p.x264-GRP", title="Ember Coast")), (), _NOW)
    assert item.title == "Ember Coast"
    assert "GRP" not in str(payload(Answer(Strip((item,)), 0.0)))


@pytest.mark.parametrize(
    ("release", "title", "told"),
    [
        ("1080p.x264-GRP", "-GRP", strip.UNNAMED),
        (
            "[GRP] Ember Coast - 01 [1080p][ABCD1234]",
            "[GRP] Ember Coast - 01 [",
            "Ember Coast - 01",
        ),
    ],
)
def test_what_is_left_of_a_name_cut_short_is_not_told_as_a_title(release, title, told):
    [item] = collect(_data(_raw(release, title=title)), (), _NOW)
    assert item.title == told


def test_such_a_release_keeps_its_card_while_nzbget_is_away_and_when_it_is_done():
    before = collect(_data(_raw(_RELEASE, title=_RELEASE)), (), _NOW)
    [held] = collect(_data(unreachable=["NZBGet"]), before, _NOW)
    assert (held.key, held.held) == (before[0].key, True)
    [ready] = collect(_data(recent=[_done(_RELEASE, 1, title=_RELEASE)]), before, _NOW)
    assert (ready.key, ready.state, ready.title) == (before[0].key, "ready", strip.UNNAMED)


def test_a_finished_one_is_ready_for_a_minute_and_then_gone():
    def items(seconds_ago):
        return collect(_data(recent=[_done("radarr:Ember Coast", seconds_ago)]), (), _NOW)

    [ready] = items(20)
    assert (ready.state, ready.percent, ready.ready_seconds) == ("ready", 100, 40)
    assert items(60) == []


def test_a_finished_one_keeps_its_place_and_the_title_it_was_known_by():
    before = collect(
        _data(_raw("radarr:A", tmdb_id=1), _raw("radarr:Ember Coast", tmdb_id=2)), (), _NOW
    )
    after = collect(
        _data(_raw("radarr:A", tmdb_id=1), recent=[_done("radarr:Ember Coast", 1)]), before, _NOW
    )
    assert [(i.title, i.state, i.tmdb_id) for i in after] == [
        ("A", "downloading", 1),
        ("Ember Coast", "ready", 2),
    ]
    assert [i.key for i in after] == [i.key for i in before]


def test_a_show_that_lacks_episodes_does_not_say_it_is_all_there():
    [item] = collect(_data(recent=[_done("sonarr:Show", 1, readiness="part")]), (), _NOW)
    assert item.line == "Some episodes ready"


def test_cards_stay_where_they_are_while_one_overtakes_another():
    before = collect(_data(_raw("radarr:A"), _raw("radarr:B")), (), _NOW)
    after = collect(_data(_raw("radarr:B"), _raw("radarr:A")), before, _NOW)
    assert [i.key for i in after] == ["radarr:A", "radarr:B"]


def test_what_waits_comes_after_what_comes_in():
    looking = _raw("radarr:Looking", state="searching", arr_source="Radarr")
    queued = _raw("nzb", state="searching")
    items = collect(_data(looking, queued, _raw("radarr:A")), (), _NOW)
    assert [i.state for i in items] == ["downloading", "queued", "searching"]


def test_what_a_service_held_stays_while_it_does_not_answer_and_goes_once_it_does():
    before = collect(_data(_raw("radarr:A"), _raw("sonarr:B", media_type="series")), (), _NOW)
    down = collect(_data(_raw("sonarr:B"), unreachable=["Radarr"]), before, _NOW)
    assert [(i.key, i.held) for i in down] == [("radarr:A", True), ("sonarr:B", False)]
    assert [i.key for i in collect(_data(_raw("sonarr:B")), down, _NOW)] == ["sonarr:B"]


def test_with_nzbget_away_a_download_is_not_shown_as_looked_for():
    before = collect(_data(_raw()), (), _NOW)
    unmatched = _raw(state="searching", arr_source="Radarr", progress=0)
    [item] = collect(_data(unmatched, unreachable=["NZBGet"]), before, _NOW)
    assert (item.state, item.percent, item.held) == ("downloading", 40, True)


def test_what_finished_while_its_service_was_away_is_not_held_as_downloading():
    before = collect(_data(_raw()), (), _NOW)
    done = _done("radarr:Ember Coast", 300)
    assert collect(_data(recent=[done], unreachable=["Radarr"]), before, _NOW) == []


def _item(key: str, state: strip.State, percent: int = 0) -> StripItem:
    return StripItem(key, key, "movie", state, percent)


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([], (0, 0, 0, 0, 0)),
        (
            [("a", "downloading", 20), ("b", "almost_ready", 100), ("c", "ready", 100)],
            (3, 2, 0, 1, 60),
        ),
        ([("a", "ready", 100), ("b", "queued", 0)], (2, 0, 1, 1, 100)),
        ([("a", "stuck", 0)], (1, 0, 1, 0, 0)),
    ],
)
def test_the_ring_is_the_mean_of_what_comes_in(items, expected):
    got = strip.summary([_item(*i) for i in items])
    assert tuple(got[k] for k in ("total", "active", "waiting", "ready", "percent")) == expected


def test_a_long_list_is_cut_and_still_counted():
    items = tuple(_item(str(n), "searching") for n in range(strip.MAX_ITEMS + 5))
    out = payload(Answer(Strip(items), 0.0))
    assert len(out["items"]) == strip.MAX_ITEMS
    assert out["summary"]["total"] == strip.MAX_ITEMS + 5


def test_a_page_is_told_how_old_the_answer_is():
    assert payload(Answer(Strip(), 2.5))["age_ms"] == 2500


def test_an_answer_held_too_long_says_it_is_how_things_were_and_drops_what_was_ready():
    items = (
        _item("a", "downloading", 10),
        StripItem("b", "b", "movie", "ready", 100, ready_seconds=20),
    )
    out = payload(Answer(Strip(items), strip.STALE_S + 1))
    assert [(i["key"], i["held"]) for i in out["items"]] == [("a", True)]
    assert out["outage"] == strip.STALE_LINE
    assert out["summary"]["ready"] == 0


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_services_are_asked_once_for_everybody_until_the_answer_is_past_its_time():
    clock, made = _Clock(), []
    shared = Shared(clock)

    def make(_before):
        made.append(1)
        return Strip((_item(str(len(made)), "queued"),))

    assert shared.get(make).strip == shared.get(make).strip
    clock.now += strip.HOLD_S
    assert shared.get(make).strip.items[0].key == "2"
    assert len(made) == 2


def test_while_one_asks_the_others_have_the_last_answer_at_once():
    clock = _Clock()
    shared = Shared(clock)
    shared.get(lambda _b: Strip((_item("old", "queued"),)))
    clock.now += strip.HOLD_S
    asked, go = threading.Event(), threading.Event()

    def slow(_before):
        asked.set()
        assert go.wait(5)
        return Strip((_item("new", "queued"),))

    asking = threading.Thread(target=lambda: shared.get(slow))
    asking.start()
    try:
        assert asked.wait(5)
        other = shared.get(lambda _b: pytest.fail("asked a second time"))
        assert other.strip.items[0].key == "old"
    finally:
        go.set()
        asking.join(5)
    assert shared.get(slow).strip.items[0].key == "new"


def test_a_failure_leaves_the_last_answer_and_is_not_tried_again_at_once():
    clock, tries = _Clock(), []
    shared = Shared(clock)
    shared.get(lambda _b: Strip((_item("a", "queued"),)))
    clock.now += strip.HOLD_S

    def broken(_before):
        tries.append(1)
        raise OSError("no")

    for _ in range(3):
        assert shared.get(broken).strip.items[0].key == "a"
    assert len(tries) == 1


def test_with_nothing_known_a_failure_is_no_empty_row():
    shared = Shared(_Clock())

    def broken(_before):
        raise OSError("no")

    with pytest.raises(strip.Unavailable):
        shared.get(broken)
    assert shared.last() is None


def test_the_page_is_not_drawn_from_an_answer_of_long_ago():
    clock = _Clock()
    shared = Shared(clock)
    shared.get(lambda _b: Strip())
    assert shared.last() is not None
    clock.now += strip.STALE_S + 1
    assert shared.last() is None


def test_what_another_has_read_stands_unless_it_is_older_than_what_stands():
    clock = _Clock()
    shared = Shared(clock)

    def of(name):
        return lambda _before: Strip((_item(name, "queued"),))

    began = shared.now()
    clock.now += 1
    shared.offer(of("later"), shared.now())
    assert shared.offer(of("older"), began).strip.items[0].key == "later"
    # No wait: the next one read stands at once.
    assert shared.offer(of("next"), shared.now()).strip.items[0].key == "next"
    assert shared.offer(lambda _b: 1 / 0, shared.now()).strip.items[0].key == "next"


def test_what_nobody_looked_at_for_a_while_is_not_held_for_a_service_that_is_down():
    clock = _Clock()
    shared = Shared(clock)
    shared.offer(lambda _b: Strip((_item("a", "queued"),)), shared.now())
    clock.now += strip.STALE_S + 1
    seen = []
    shared.offer(lambda before: seen.append(before) or Strip(), shared.now())
    assert seen == [Strip()]


def test_what_is_offered_while_one_is_being_made_is_left_and_nobody_waits():
    shared = Shared(_Clock())

    def make(_before):
        assert shared.offer(lambda _b: pytest.fail("made twice"), shared.now()) is None
        return Strip()

    shared.offer(make, shared.now())


def test_a_payload_asked_for_without_saying_who_tells_nobody_of_a_refused_key():
    told = strip.Strip(outage="Can't sign in to Radarr", refused=True)

    said = strip.payload(strip.Answer(told, 0.0))

    assert said["outage"] == VIEWER_OUTAGE
    assert strip.payload(strip.Answer(told, 0.0), admin=True)["outage"] == told.outage
