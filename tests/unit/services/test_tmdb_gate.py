"""A TMDB that hangs or fails holds few requests and for a short time, and none once it is
known to be down."""

from __future__ import annotations

import threading

import pytest

from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.watch_catalog import _gate, _library

ASKS = {
    "explore": lambda c: catalog.explore_page(c, catalog.GENRES["horror"]),
    "person": lambda c: catalog.person(c, 7),
    "season": lambda c: catalog.season(c, 7, 1),
    "search": lambda c: catalog.search(c, "glass"),
    "rows": catalog.discover_shelves,
    "page": lambda c: catalog.discover_page(c, "trending", 2),
    "title": lambda c: catalog.details(c, "movie", 5),
}


class Tmdb(TmdbClient):
    def __init__(self, token="token") -> None:
        super().__init__(token)
        self.status = 0
        self.asked = 0

    def _get(self, path, params=None):
        self.asked += 1
        if self.status:
            raise SafeHTTPError(status_code=self.status, body_snippet="", url="https://t.invalid")
        return {
            "name": "Mara",
            "title": "Glass",
            "results": [{"id": 1, "media_type": "movie"}],
            "episodes": [],
        }


@pytest.fixture
def tmdb(monkeypatch, tmp_path):
    disk_cache.init(tmp_path)
    monkeypatch.setattr(disk_cache, "sync", True)
    monkeypatch.setattr(_library, "_tvdb", {})
    return Tmdb()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(_gate, "_clock", lambda: now[0])
    monkeypatch.setattr(disk_cache.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def hung(tmdb, monkeypatch):
    """As many requests waiting on TMDB as may."""
    release, waiting = threading.Event(), threading.Semaphore(0)
    answer = tmdb._get

    def hang(path, params=None):
        waiting.release()
        release.wait(10)
        return answer(path, params)

    monkeypatch.setattr(tmdb, "_get", hang)
    threads = [
        # Two for each title: one that waits on another's ask waits as well.
        threading.Thread(target=catalog.details, args=(tmdb, "movie", 100 + n // 2), daemon=True)
        for n in range(_gate.SLOTS)
    ]
    for thread in threads:
        thread.start()
    for _ in range(_gate.SLOTS // 2):
        assert waiting.acquire(timeout=5)
    while _gate.gate._free:
        threading.Event().wait(0.001)
    monkeypatch.setattr(tmdb, "_get", lambda *_: pytest.fail("one more waits on TMDB"))
    yield tmdb
    release.set()
    for thread in threads:
        thread.join(10)


@pytest.mark.parametrize("ask", ASKS.values(), ids=ASKS)
def test_while_enough_wait_on_tmdb_the_next_is_refused_at_once(hung, ask):
    with pytest.raises(catalog.TmdbUnavailable):
        ask(hung)


def test_a_refused_one_is_told_to_try_again_and_spends_no_limit(hung):
    spent = []

    with pytest.raises(catalog.TmdbBusy):
        catalog.person(hung, 7, allow_fetch=lambda: bool(spent.append(1)))

    assert spent == []


def test_a_show_looked_up_by_its_tvdb_id_does_not_wait_either(hung):
    assert catalog.tmdb_id_for_tvdb(hung, 5500) is None


def test_what_is_cached_answers_meanwhile(tmdb, clock):
    catalog.person(tmdb, 7)
    tmdb.status = 500
    for n in (1, 2):
        with pytest.raises(catalog.TmdbDown):
            catalog.details(tmdb, "movie", n)

    assert catalog.person(tmdb, 7)["name"] == "Mara"


def test_the_place_of_one_who_waited_is_free_again(tmdb):
    tmdb.status = 404
    for n in range(_gate.SLOTS + 1):
        with pytest.raises(catalog.TitleNotFound):
            catalog.details(tmdb, "movie", n)


@pytest.mark.parametrize("failing", ASKS.values(), ids=ASKS)
@pytest.mark.parametrize("status", [500, 429])
def test_after_two_failures_nobody_asks(tmdb, clock, failing, status):
    tmdb.status = status
    catalog_ids = iter(range(50, 60))
    with pytest.raises(catalog.TmdbUnavailable):
        failing(tmdb)
    with pytest.raises(catalog.TmdbUnavailable):
        catalog.details(tmdb, "movie", next(catalog_ids))
    asked = tmdb.asked

    for n in catalog_ids:
        with pytest.raises(catalog.TmdbUnavailable):
            catalog.details(tmdb, "movie", n)

    assert tmdb.asked == asked


def _fail_twice(tmdb, status=500):
    tmdb.status = status
    for n in (1, 2):
        with pytest.raises(catalog.TmdbUnavailable):
            catalog.details(tmdb, "movie", n)
    tmdb.status = 0


def test_trying_again_asks_once_every_five_seconds_and_an_answer_ends_the_pause(tmdb, clock):
    _fail_twice(tmdb)

    with pytest.raises(catalog.TmdbDown):
        catalog.details(tmdb, "movie", 1)
    clock[0] += 5
    tmdb.status = 500
    for _ in range(3):
        with pytest.raises(catalog.TmdbDown):
            catalog.details(tmdb, "movie", 1)
    assert tmdb.asked == 3

    clock[0] += 5
    tmdb.status = 0
    assert catalog.details(tmdb, "movie", 1)["title"] == "Glass"
    assert catalog.person(tmdb, 7)["name"] == "Mara"


def test_only_one_at_a_time_tries_whether_tmdb_is_back(tmdb, clock):
    _fail_twice(tmdb)
    clock[0] += 5

    with _gate.gate.asking(tmdb.token), pytest.raises(catalog.TmdbDown):
        catalog.details(tmdb, "movie", 9)


def test_a_page_that_failed_once_may_be_asked_for_again_after_five_seconds(tmdb, clock):
    tmdb.status = 500
    for _ in range(2):
        with pytest.raises(catalog.TmdbDown):
            catalog.discover_page(tmdb, "trending", 2)
    assert tmdb.asked == 1

    clock[0] += 5
    tmdb.status = 0
    assert catalog.discover_page(tmdb, "trending", 2)[0]


def test_trying_again_is_counted_against_the_persons_limit(tmdb, clock):
    _fail_twice(tmdb)
    clock[0] += 5

    with pytest.raises(disk_cache.Refused):
        catalog.discover_page(tmdb, "trending", 2, allow_fetch=lambda: False)


def test_a_preview_does_not_try_again_before_the_pause_is_over(tmdb, clock):
    _fail_twice(tmdb)

    clock[0] += _gate.PROBE_S
    with pytest.raises(catalog.TmdbDown):
        catalog.details(tmdb, "movie", 9, brief=True)
    clock[0] += _gate.PAUSE_S - _gate.PROBE_S
    assert catalog.details(tmdb, "movie", 9, brief=True)["title"] == "Glass"


def test_a_new_token_is_not_paused_by_what_the_old_one_was_refused(tmdb, clock):
    _fail_twice(tmdb, 429)

    assert catalog.details(Tmdb("new"), "movie", 9)["title"] == "Glass"


def test_a_stale_entry_is_not_refreshed_while_tmdb_is_paused(tmdb, clock, monkeypatch):
    catalog.person(tmdb, 7)
    _fail_twice(tmdb)
    later = disk_cache.time.time() + 8 * 86400
    monkeypatch.setattr(disk_cache.time, "time", lambda: later)
    asked = tmdb.asked

    assert catalog.person(tmdb, 7)["name"] == "Mara"
    assert tmdb.asked == asked


def test_the_job_that_fills_the_library_neither_waits_its_turn_nor_pauses_anyone(tmdb, clock):
    catalog.person(tmdb, 8)
    tmdb.status = 500
    for n in (1, 2):
        with pytest.raises(catalog.TmdbDown):
            catalog.details(tmdb, "movie", n, brief=True, background=True)
    tmdb.status = 0
    assert catalog.person(tmdb, 7)["name"] == "Mara"

    _fail_twice(tmdb)
    assert catalog.details(tmdb, "movie", 9, background=True)["title"] == "Glass"


def test_a_season_not_waited_for_is_asked_for_in_the_background(tmdb, clock):
    with pytest.raises(disk_cache.Refused):
        catalog.season(tmdb, 7, 1, wait=False)

    assert catalog.season(tmdb, 7, 1, wait=False)["episodes"] == {}
    assert tmdb.asked == 1


def test_a_season_not_waited_for_asks_nothing_of_a_tmdb_that_is_down(tmdb, clock):
    tmdb.status = 500
    for n in (1, 2):
        with pytest.raises(catalog.TmdbDown):
            catalog.details(tmdb, "movie", n)
    asked = tmdb.asked

    with pytest.raises(disk_cache.Refused):
        catalog.season(tmdb, 7, 1, wait=False)

    assert tmdb.asked == asked
