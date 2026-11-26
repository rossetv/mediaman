"""Downloads Sonarr or Radarr will not import, removed and searched for again by the poller."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.fetcher import _forget_answers, fetch_arr_queue
from mediaman.services.arr.finding_copy import is_finding_copy
from mediaman.services.arr.search_trigger import reset_search_triggers, trigger_pending_searches
from mediaman.services.arr.seasons import queued_seasons
from mediaman.services.arr.spec import RADARR_SPEC, SONARR_SPEC
from mediaman.services.downloads import strip
from mediaman.services.downloads.download_queue.items import build_unmatched_arr_item
from mediaman.web.repository import history
from tests.conftest import _fake_response
from tests.helpers.factories import insert_settings

_START = datetime(2026, 1, 10, 12, tzinfo=UTC)
_KEY = "x" * 64
_REASON = "No files found are eligible for import"


def _stuck_episode(queue_id: int, episode: int, *, download: str = "pack-1", **overrides):
    return {
        "id": queue_id,
        "seriesId": 5,
        "episodeId": 500 + episode,
        "title": "Alone.S13.1080p",
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [{"title": "Alone.S13.1080p", "messages": [_REASON]}],
        "size": 1000,
        "sizeleft": 0,
        "downloadId": download,
        "series": {"id": 5, "title": "Alone", "year": 2015, "tmdbId": 61, "images": []},
        "episode": {"id": 500 + episode, "seasonNumber": 13, "episodeNumber": episode},
        **overrides,
    }


def _stuck_film(queue_id: int, **overrides):
    return {
        "id": queue_id,
        "movieId": 9,
        "title": "Dune.2021.1080p",
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importBlocked",
        "statusMessages": [{"title": "Dune.2021.1080p", "messages": ["Not a sample"]}],
        "size": 1000,
        "sizeleft": 0,
        "downloadId": f"film-{queue_id}",
        "movie": {"id": 9, "title": "Dune", "year": 2021, "tmdbId": 438631, "images": []},
        **overrides,
    }


class Arrs:
    """A Sonarr and a Radarr answering over stubbed HTTP, on a real database, with a clock
    stepped by hand."""

    def __init__(self, conn, fake_http, monkeypatch) -> None:
        self.conn = conn
        self.now = _START
        self.queues: dict[str, list[dict]] = {"sonarr": [], "radarr": []}
        self.movies: list[dict] = []
        self.calls: list[tuple[str, str, str, object]] = []
        self.failing: set[str] = set()
        # Queue ids whose DELETE raises the error given.
        self.failing_ids: dict[int, Exception] = {}
        # Whether a DELETE leaves its record in the queue, as a download client that would not
        # drop it does.
        self.keeping = False
        fake_http.handler(self._answer)
        clients = {
            "sonarr": ArrClient(SONARR_SPEC, "http://sonarr.test:8989", "k"),
            "radarr": ArrClient(RADARR_SPEC, "http://radarr.test:7878", "k"),
        }
        for name, client in clients.items():
            monkeypatch.setattr(
                f"mediaman.services.arr.build.build_{name}_from_db",
                lambda *a, client=client: client,
            )
        for module in ("import_retry", "fetcher._searching_since"):
            monkeypatch.setattr(f"mediaman.services.arr.{module}.now_utc", lambda: self.now)

    def _answer(self, method, url, **kwargs):
        parts = urlsplit(url)
        service = "sonarr" if parts.hostname == "sonarr.test" else "radarr"
        body = kwargs.get("json")
        if method != "GET":
            self.calls.append((method, service, parts.path, parse_qs(parts.query) or body))
        if method in self.failing:
            raise requests.ConnectionError("refused")
        if parts.path == "/api/v3/queue":
            records = self.queues[service]
            return _fake_response(
                json_data={"page": 1, "totalRecords": len(records), "records": records}
            )
        if method == "DELETE" and parts.path.startswith("/api/v3/queue/"):
            queue_id = int(parts.path.rsplit("/", 1)[1])
            if queue_id in self.failing_ids:
                raise self.failing_ids[queue_id]
            if self.keeping:
                return _fake_response(json_data={})
            self.queues[service] = [q for q in self.queues[service] if q["id"] != queue_id]
            return _fake_response(json_data={})
        if parts.path == "/api/v3/movie":
            return _fake_response(json_data=self.movies)
        if parts.path == "/api/v3/series":
            return _fake_response(json_data=[])
        if parts.path == "/api/v3/wanted/missing":
            return _fake_response(json_data={"page": 1, "totalRecords": 0, "records": []})
        if method == "POST" and parts.path == "/api/v3/command":
            return _fake_response(json_data={"id": 1, **(body or {})})
        raise AssertionError(f"unexpected {method} {url}")

    def poll(self, after: timedelta = timedelta()) -> None:
        """Let *after* go by, then let the hourly poller run."""
        self.now += after
        _forget_answers()
        trigger_pending_searches(self.conn, _KEY)

    def changes(self) -> list[tuple[str, str, str, object]]:
        """The DELETEs and commands sent since last asked, searches for stalled items left out."""
        made = [c for c in self.calls if not (c[2] == "/api/v3/command" and _poke(c[3]))]
        self.calls.clear()
        return made

    def history(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT detail FROM audit_log WHERE action='import_retried' ORDER BY id"
        )
        return [r[0] for r in rows]

    def states(self) -> dict[str, str]:
        _forget_answers()
        cards = fetch_arr_queue(self.conn, _KEY)
        return {
            c["title"]: build_unmatched_arr_item(c, {}, lambda *a, **k: "", lambda *a: "")["state"]
            for c in cards
        }


def _poke(command: object) -> bool:
    return isinstance(command, dict) and command.get("name") in ("SeriesSearch",)


@pytest.fixture
def arrs(tmp_path, fake_http, monkeypatch):
    reset_search_triggers()
    _forget_answers()
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield Arrs(conn, fake_http, monkeypatch)
    conn.close()
    _forget_answers()


def _season_stuck(arrs: Arrs, first_id: int, download: str = "pack-1") -> None:
    arrs.queues["sonarr"] = [
        _stuck_episode(first_id, 1, download=download),
        _stuck_episode(first_id + 1, 2, download=download),
    ]


def test_a_download_stuck_under_an_hour_is_left_alone(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(minutes=50))

    assert arrs.changes() == []
    assert arrs.states() == {"Alone": "needs_attention"}


def test_a_season_stuck_an_hour_is_removed_blocklisted_and_searched_again(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    flags = {"removeFromClient": ["true"], "blocklist": ["true"], "skipRedownload": ["true"]}
    assert arrs.changes() == [
        ("DELETE", "sonarr", "/api/v3/queue/10", flags),
        ("DELETE", "sonarr", "/api/v3/queue/11", flags),
        (
            "POST",
            "sonarr",
            "/api/v3/command",
            {"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 13},
        ),
    ]
    assert arrs.history() == [
        f"'Alone' S13 couldn't be imported ({_REASON}); removed and searching for another copy"
        " (try 1 of 2)"
    ]
    # The show has episodes in the library, so Sonarr lists it nowhere while it searches.
    assert arrs.states() == {"Alone": "finding_copy"}


def test_episodes_of_separate_downloads_are_searched_for_by_episode(arrs):
    arrs.queues["sonarr"] = [
        _stuck_episode(10, 1, download="a"),
        _stuck_episode(11, 2, download="b"),
    ]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert arrs.changes()[-1][3] == {"name": "EpisodeSearch", "episodeIds": [501, 502]}


def test_a_new_grab_ends_finding_another_copy_and_a_stuck_one_is_tried_again(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.queues["sonarr"] = [
        _stuck_episode(
            20,
            1,
            trackedDownloadStatus="ok",
            trackedDownloadState="downloading",
            sizeleft=500,
            status="downloading",
        )
    ]
    assert arrs.states() == {"Alone": "downloading"}

    _season_stuck(arrs, 30, download="pack-2")
    arrs.poll(timedelta(minutes=5))
    arrs.changes()
    arrs.poll(timedelta(hours=1))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == [
        "/api/v3/queue/30",
        "/api/v3/queue/31",
    ]
    assert arrs.history()[-1].endswith("(try 2 of 2)")


def test_a_third_stuck_copy_is_left_for_an_admin(arrs):
    for first in (10, 30):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=1))
    _season_stuck(arrs, 50, download="pack-50")
    arrs.poll(timedelta(minutes=5))
    arrs.changes()
    arrs.poll(timedelta(hours=1))
    arrs.poll(timedelta(hours=5))

    assert arrs.changes() == []
    assert len(arrs.history()) == 2
    assert arrs.states() == {"Alone": "needs_attention"}


def test_with_the_setting_off_nothing_is_removed(arrs):
    insert_settings(arrs.conn, import_retry_enabled="false")
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=2))

    assert arrs.changes() == []
    assert arrs.history() == []


def test_a_restart_keeps_the_clock_and_the_count(arrs, tmp_path):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.conn.close()
    arrs.conn = init_db(str(tmp_path / "mediaman.db"))
    arrs.poll(timedelta(hours=1))
    assert len(arrs.history()) == 1

    _season_stuck(arrs, 30, download="pack-2")
    arrs.poll(timedelta(minutes=5))
    arrs.conn.close()
    arrs.conn = init_db(str(tmp_path / "mediaman.db"))
    arrs.poll(timedelta(hours=1))

    assert arrs.history()[-1].endswith("(try 2 of 2)")


def test_a_failed_removal_is_no_try_and_is_made_again_next_run(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.failing = {"DELETE"}
    arrs.poll(timedelta(hours=1))
    assert arrs.history() == []
    assert arrs.states() == {"Alone": "needs_attention"}

    arrs.failing = set()
    arrs.changes()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert arrs.history()[-1].endswith("(try 1 of 2)")


def test_a_failed_search_is_no_try_and_is_sent_next_run(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.failing = {"POST"}
    arrs.poll(timedelta(hours=1))
    assert arrs.history() == []

    arrs.failing = set()
    arrs.changes()
    arrs.poll(timedelta(hours=1))

    assert arrs.changes() == [
        (
            "POST",
            "sonarr",
            "/api/v3/command",
            {"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 13},
        )
    ]
    assert arrs.history() == [arrs.history()[0]]
    assert arrs.history()[0].endswith("(try 1 of 2)")


def test_downloading_and_healthy_imports_are_never_touched(arrs):
    arrs.queues["sonarr"] = [
        _stuck_episode(
            10, 1, status="downloading", trackedDownloadState="downloading", sizeleft=400
        ),
        _stuck_episode(11, 2),
    ]
    arrs.queues["radarr"] = [
        _stuck_film(40, trackedDownloadStatus="ok", trackedDownloadState="importPending")
    ]
    arrs.poll()
    arrs.poll(timedelta(hours=3))

    assert arrs.changes() == []


def test_a_film_stuck_an_hour_is_removed_and_searched_for(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [
        {
            "id": 9,
            "title": "Dune",
            "year": 2021,
            "tmdbId": 438631,
            "monitored": True,
            "hasFile": False,
            "added": "2025-01-01T00:00:00Z",
        }
    ]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    made = arrs.changes()
    assert made[0] == (
        "DELETE",
        "radarr",
        "/api/v3/queue/40",
        {"removeFromClient": ["true"], "blocklist": ["true"], "skipRedownload": ["true"]},
    )
    assert ("POST", "radarr", "/api/v3/command", {"name": "MoviesSearch", "movieIds": [9]}) in made
    assert arrs.history()[0].startswith("'Dune' couldn't be imported (Not a sample)")
    assert arrs.states() == {"Dune": "finding_copy"}


def test_a_viewer_reads_finding_another_copy_and_nothing_of_what_sonarr_says(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    _forget_answers()
    card = fetch_arr_queue(arrs.conn, _KEY)[0]
    item = build_unmatched_arr_item(card, {}, lambda *a, **k: "", lambda *a: "")
    data = {"hero": item, "queue": [], "recent": [], "unreachable": []}
    items = tuple(strip.collect(data, (), _START))
    told = json.dumps(strip.payload(strip.Answer(strip=strip.Strip(items=items), age_s=0)))

    def downloads(is_admin: bool) -> list[str]:
        viewer = history.Viewer(1, "someone", "2020-01-01T00:00:00Z", is_admin)
        page = history.history_page(arrs.conn, viewer, history.Filters(action="downloads"))
        return [e.detail for e in page.entries]

    assert item["state_label"] == "Finding another copy"
    assert '"state": "finding_copy"' in told and _REASON not in told
    assert downloads(is_admin=True) == arrs.history()
    assert downloads(is_admin=False) == []


def _dune(**overrides):
    return {
        "id": 9,
        "title": "Dune",
        "year": 2021,
        "tmdbId": 438631,
        "monitored": True,
        "hasFile": False,
        "added": "2025-01-01T00:00:00Z",
        **overrides,
    }


def _hourly(arrs: Arrs, hours: int) -> None:
    for _ in range(hours):
        arrs.poll(timedelta(hours=1))


@pytest.mark.parametrize("monitored", [True, False])
def test_a_film_whose_download_stays_in_the_queue_is_tried_twice_in_all(arrs, monitored):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune(monitored=monitored)]
    arrs.keeping = True
    arrs.poll()
    _hourly(arrs, 80)

    assert [line[-12:] for line in arrs.history()] == ["(try 1 of 2)", "(try 2 of 2)"]
    assert arrs.states() == {"Dune": "needs_attention"}


def test_a_retry_that_grabs_nothing_does_not_give_its_try_back(arrs):
    for first in (10, 30, 50):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=1))
        arrs.queues["sonarr"] = []
        _hourly(arrs, 26)

    assert len(arrs.history()) == 2


def test_a_stuck_copy_removed_by_someone_does_not_give_the_tries_back(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    for first in (30, 50):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.queues["sonarr"] = []
        arrs.poll(timedelta(minutes=5))
    _season_stuck(arrs, 70, download="pack-70")
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))
    _season_stuck(arrs, 90, download="pack-90")
    arrs.poll(timedelta(minutes=5))
    _hourly(arrs, 3)

    assert len(arrs.history()) == 2


def test_a_copy_that_imports_lets_a_later_stuck_one_be_tried_afresh(arrs):
    for first in (10, 30):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=1))
    arrs.queues["sonarr"] = [
        _stuck_episode(
            60,
            1,
            trackedDownloadStatus="ok",
            status="downloading",
            trackedDownloadState="downloading",
            sizeleft=10,
        )
    ]
    arrs.poll(timedelta(minutes=5))
    arrs.queues["sonarr"] = []
    arrs.poll(timedelta(minutes=5))
    _season_stuck(arrs, 80, download="pack-80")
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))

    assert arrs.history()[-1].endswith("(try 1 of 2)")
    assert len(arrs.history()) == 3


def test_two_stuck_records_of_one_film_are_removed_together_and_searched_for_once(arrs):
    arrs.queues["radarr"] = [_stuck_film(40), _stuck_film(41)]
    arrs.movies = [_dune()]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    made = arrs.changes()
    assert [c[2] for c in made if c[0] == "DELETE"] == ["/api/v3/queue/40", "/api/v3/queue/41"]
    assert [c[3] for c in made if c[0] == "POST"] == [{"name": "MoviesSearch", "movieIds": [9]}]
    assert len(arrs.history()) == 1


def test_a_film_with_another_copy_still_downloading_is_left_alone(arrs):
    arrs.queues["radarr"] = [
        _stuck_film(40),
        _stuck_film(
            41,
            status="downloading",
            trackedDownloadStatus="ok",
            trackedDownloadState="downloading",
            sizeleft=400,
        ),
    ]
    arrs.movies = [_dune()]
    arrs.poll()
    _hourly(arrs, 3)

    assert arrs.changes() == []


@pytest.mark.parametrize(
    "error",
    [requests.ReadTimeout("no answer"), requests.ConnectionError("refused")],
    ids=["timed-out", "refused"],
)
def test_a_removal_failing_after_one_record_went_still_searches_and_says_so(arrs, error):
    _season_stuck(arrs, 10, download="a")
    arrs.queues["sonarr"][1]["downloadId"] = "b"
    arrs.failing_ids = {11: error}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[3] for c in arrs.changes() if c[0] == "POST"] == [
        {"name": "EpisodeSearch", "episodeIds": [501, 502]}
    ]
    assert arrs.history()[-1].endswith("(try 1 of 2)")


def test_a_removal_that_timed_out_may_have_gone_through_so_the_search_is_sent(arrs):
    _season_stuck(arrs, 10)
    arrs.failing_ids = {10: requests.ReadTimeout("no answer")}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()][-1] == "POST"
    assert arrs.history()[-1].endswith("(try 1 of 2)")


@pytest.mark.parametrize(
    "message",
    [
        "[/downloads/Alone.S13] is not a valid local path. You may need a Remote Path Mapping.",
        "Failed to import episode due to permission issues.",
        "Failed to import episode, Root folder missing.",
        "Not enough free space",
    ],
)
def test_a_download_stuck_on_sonarrs_own_setup_is_left_for_an_admin(arrs, message):
    _season_stuck(arrs, 10)
    for record in arrs.queues["sonarr"]:
        record["statusMessages"] = [{"title": "Alone.S13.1080p", "messages": [message]}]
    arrs.poll()
    _hourly(arrs, 3)

    assert arrs.changes() == []
    assert arrs.states() == {"Alone": "needs_attention"}


def test_records_of_a_season_that_import_or_wait_are_spared_beside_a_stuck_one(arrs):
    arrs.queues["sonarr"] = [
        _stuck_episode(10, 1, download="a"),
        _stuck_episode(11, 2, download="b", trackedDownloadStatus="ok"),
        _stuck_episode(
            12,
            3,
            download="c",
            status="queued",
            trackedDownloadStatus="ok",
            trackedDownloadState="downloading",
            sizeleft=1000,
        ),
    ]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == ["/api/v3/queue/10"]


def test_a_season_still_downloading_is_not_called_stuck_by_another_record_of_it():
    def entry(status: str, tracked_status: str, tracked_state: str, left: int) -> dict:
        return {
            "series": {"id": 5},
            "episode": {"seasonNumber": 13},
            "size": 1000,
            "sizeleft": left,
            "status": status,
            "trackedDownloadStatus": tracked_status,
            "trackedDownloadState": tracked_state,
        }

    downloading = entry("downloading", "ok", "downloading", 400)
    stuck = entry("completed", "warning", "importPending", 0)

    assert queued_seasons([downloading, stuck]) == {(5, 13): "downloading"}
    assert queued_seasons([stuck, downloading]) == {(5, 13): "downloading"}


def test_another_season_downloading_shows_beside_one_being_retried(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    next_season = _stuck_episode(
        20,
        1,
        download="s14",
        status="downloading",
        trackedDownloadStatus="ok",
        trackedDownloadState="downloading",
        sizeleft=400,
        episodeId=601,
    )
    next_season["episode"] = {"id": 601, "seasonNumber": 14, "episodeNumber": 1}
    arrs.queues["sonarr"].append(next_season)

    assert arrs.states() == {"Alone": "downloading"}
    assert is_finding_copy(arrs.conn, "sonarr", 5, 13)


def test_turning_the_setting_off_mid_retry_stops_finding_another_copy(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    insert_settings(arrs.conn, import_retry_enabled="false")

    assert "finding_copy" not in arrs.states().values()
    assert not is_finding_copy(arrs.conn, "sonarr", 5)
