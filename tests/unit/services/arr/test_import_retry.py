"""Downloads Sonarr or Radarr will not import, removed and searched for again by the poller."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.fetcher import _forget_answers, fetch_arr_queue
from mediaman.services.arr.finding_copy import fixing_words, is_finding_copy
from mediaman.services.arr.import_retry import _mark_fixed
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
        # Queue ids whose DELETE raises the error given, or is answered with the status given.
        self.failing_ids: dict[int, Exception | int] = {}
        # When Sonarr added the file of each of Alone's episodes that has one, by episode id.
        self.files: dict[int, str] = {}
        # The GETs of a film or a show, by service and path.
        self.reads: list[tuple[str, str]] = []
        # Services whose film and show reads time out.
        self.slow: set[str] = set()
        # Paths of a film or a show answered with a 500.
        self.broken: set[str] = set()
        # Reads of a film or a show, with their query, that time out.
        self.hanging: set[str] = set()
        # The status a command is refused with, if it is.
        self.refusing = 0
        # Whether a DELETE leaves its record in the queue, as a download client that would not
        # drop it does.
        self.keeping = False
        # What each service says it is, None for one that will not say, and whether its
        # "Redownload failed" is on: older ones ignore skipRedownload and search by themselves.
        self.versions: dict[str, str | None] = {"sonarr": "4.0.14.2939", "radarr": "5.26.2.10099"}
        self.redownload = True
        self.own_searches: list[str] = []
        self.version_reads = 0
        # What GET /api/v3/manualimport lists, and the queries it was asked with.
        self.manual: list[dict] = []
        self.manual_asked: list[dict] = []
        # The error each command, by name, is answered with.
        self.breaking: dict[str, Exception] = {}
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
        for module in ("import_retry", "finding_copy", "fetcher._searching_since"):
            monkeypatch.setattr(f"mediaman.services.arr.{module}.now_utc", lambda: self.now)

    def _answer(self, method, url, **kwargs):
        parts = urlsplit(url)
        service = "sonarr" if parts.hostname == "sonarr.test" else "radarr"
        body = kwargs.get("json")
        if method != "GET":
            self.calls.append((method, service, parts.path, parse_qs(parts.query) or body))
        if method in self.failing:
            raise requests.ConnectionError("refused")
        if method == "GET" and parts.path.startswith(
            ("/api/v3/movie/", "/api/v3/series/", "/api/v3/episode")
        ):
            self.reads.append((service, f"{parts.path}?{parts.query}".rstrip("?")))
            if service in self.slow:
                raise requests.ReadTimeout("no answer")
            if parts.path in self.broken:
                return _fake_response(status=500, json_data={})
            if self.reads[-1][1] in self.hanging:
                raise requests.ReadTimeout("no answer")
        if parts.path == "/api/v3/system/status":
            self.version_reads += 1
            version = self.versions[service]
            if version is None:
                return _fake_response(status=500, json_data={})
            return _fake_response(json_data={"appName": service.capitalize(), "version": version})
        if parts.path == "/api/v3/config/downloadclient":
            return _fake_response(json_data={"autoRedownloadFailed": self.redownload})
        if parts.path == "/api/v3/queue":
            records = self.queues[service]
            return _fake_response(
                json_data={"page": 1, "totalRecords": len(records), "records": records}
            )
        if method == "DELETE" and parts.path.startswith("/api/v3/queue/"):
            queue_id = int(parts.path.rsplit("/", 1)[1])
            if isinstance(failure := self.failing_ids.get(queue_id), Exception):
                raise failure
            if failure:
                return _fake_response(status=failure, json_data={})
            removed = [q for q in self.queues[service] if q["id"] == queue_id]
            older = self._ignores_skip_redownload(service)
            if older and not removed:
                # It failed the whole download, so its other records are gone with it.
                return _fake_response(status=404, json_data={})
            if self.redownload and (
                older or parse_qs(parts.query).get("skipRedownload") != ["true"]
            ):
                self.own_searches.append(service)
            if self.keeping:
                return _fake_response(json_data={})
            gone = {q["downloadId"] for q in removed} if older else set()
            self.queues[service] = [
                q
                for q in self.queues[service]
                if q["id"] != queue_id and q["downloadId"] not in gone
            ]
            return _fake_response(json_data={})
        if parts.path == "/api/v3/movie":
            return _fake_response(json_data=self.movies)
        if parts.path == "/api/v3/series":
            return _fake_response(json_data=[])
        if parts.path.startswith("/api/v3/movie/"):
            movie_id = int(parts.path.rsplit("/", 1)[1])
            found = [m for m in self.movies if m["id"] == movie_id]
            return _fake_response(json_data=found[0]) if found else _fake_response(status=404)
        if parts.path == "/api/v3/series/5":
            stats = {"episodeFileCount": len(self.files), "episodeCount": 2}
            season = {"seasonNumber": 13, "monitored": True, "statistics": stats}
            return _fake_response(json_data={"id": 5, "title": "Alone", "seasons": [season]})
        if parts.path.startswith("/api/v3/series/"):
            return _fake_response(status=404)
        if parts.path == "/api/v3/episode":
            asked = parse_qs(parts.query)
            assert asked["seriesId"] == ["5"]
            if asked["seasonNumber"] != ["13"]:
                return _fake_response(json_data=[])
            episodes = [{"id": 500 + n, "seasonNumber": 13, "episodeNumber": n} for n in (1, 2)]
            for ep in episodes:
                if ep["id"] in self.files:
                    ep.update(hasFile=True, episodeFileId=ep["id"] + 1000)
            return _fake_response(json_data=episodes)
        if parts.path.startswith("/api/v3/episodefile/"):
            episode = int(parts.path.rsplit("/", 1)[1]) - 1000
            return _fake_response(
                json_data={"id": episode + 1000, "dateAdded": self.files[episode]}
            )
        if parts.path == "/api/v3/manualimport":
            self.manual_asked.append(parse_qs(parts.query))
            return _fake_response(json_data=self.manual)
        if parts.path == "/api/v3/wanted/missing":
            return _fake_response(json_data={"page": 1, "totalRecords": 0, "records": []})
        if method == "POST" and parts.path == "/api/v3/command":
            if self.refusing:
                return _fake_response(status=self.refusing, json_data={})
            if (body or {}).get("name") in self.breaking:
                raise self.breaking[body["name"]]
            return _fake_response(json_data={"id": 1, **(body or {})})
        raise AssertionError(f"unexpected {method} {url}")

    def _ignores_skip_redownload(self, service: str) -> bool:
        said = self.versions[service]
        if said is None:
            return False
        version = tuple(int(n) for n in said.split("."))
        return version < ((4,) if service == "sonarr" else (4, 7, 2))

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

    def at(self, after: timedelta = timedelta()) -> str:
        """The time *after* from now, as Radarr and Sonarr write it."""
        return (self.now + after).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        f"'Alone' S13 couldn't be imported ({_REASON}); blocklisted that copy and searching for another"
        " (try 1)"
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
    arrs.poll(timedelta(hours=3))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == [
        "/api/v3/queue/30",
        "/api/v3/queue/31",
    ]
    assert arrs.history()[-1].endswith("(try 2)")


def test_each_next_copy_is_left_longer_and_the_tries_never_stop(arrs):
    for n, hours in enumerate((1, 3, 12, 24, 24)):
        _season_stuck(arrs, 10 + 20 * n, download=f"pack-{n}")
        arrs.poll(timedelta(minutes=5))
        arrs.changes()
        arrs.poll(timedelta(hours=hours, minutes=-10))
        assert [c for c in arrs.changes() if c[0] == "DELETE"] == [], hours
        arrs.poll(timedelta(minutes=10))
        assert len([c for c in arrs.changes() if c[0] == "DELETE"]) == 2, hours

    assert [line[line.rindex("(try ") :] for line in arrs.history()] == [
        f"(try {n})" for n in range(1, 6)
    ]


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
    arrs.poll(timedelta(hours=3))

    assert arrs.history()[-1].endswith("(try 2)")


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
    assert arrs.history()[-1].endswith("(try 1)")


def test_a_failed_search_is_sent_next_run_and_written_once(arrs):
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
    assert arrs.history()[0].endswith("(try 1)")


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
def test_a_film_whose_download_stays_in_the_queue_is_tried_again_after_longer_waits(
    arrs, monitored
):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune(monitored=monitored)]
    arrs.keeping = True
    arrs.poll()
    _hourly(arrs, 80)

    # The first try, the second a day of looking and 3 hours later, the third a day and 12 later.
    assert [line[line.rindex("(try ") :] for line in arrs.history()] == [
        "(try 1)",
        "(try 2)",
        "(try 3)",
    ]
    assert arrs.states() == {"Dune": "finding_copy"}


def test_a_retry_that_grabs_nothing_does_not_give_its_try_back(arrs):
    for first, hours in ((10, 1), (30, 3), (50, 12)):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=hours))
        arrs.queues["sonarr"] = []
        _hourly(arrs, 26)

    assert [line[line.rindex("(try ") :] for line in arrs.history()] == [
        "(try 1)",
        "(try 2)",
        "(try 3)",
    ]


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
    assert arrs.history()[-1].endswith("(try 1)")


def test_a_removal_that_timed_out_may_have_gone_through_so_the_search_is_sent(arrs):
    _season_stuck(arrs, 10)
    arrs.failing_ids = {10: requests.ReadTimeout("no answer")}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()][-1] == "POST"
    assert arrs.history()[-1].endswith("(try 1)")


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


def _downloading(record: dict) -> dict:
    return {
        **record,
        "status": "downloading",
        "trackedDownloadStatus": "ok",
        "trackedDownloadState": "downloading",
        "sizeleft": 400,
    }


def test_a_season_imported_between_two_runs_ends_finding_and_gets_its_tries_back(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.files = {501: arrs.at(timedelta(minutes=30)), 502: arrs.at(timedelta(minutes=30))}
    arrs.poll(timedelta(hours=1))

    assert "finding_copy" not in arrs.states().values()
    assert not is_finding_copy(arrs.conn, "sonarr", 5)
    _season_stuck(arrs, 30, download="pack-2")
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))
    assert arrs.history()[-1].endswith("(try 1)")


def test_a_film_imported_between_two_runs_gets_its_tries_back(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.movies = [
        _dune(hasFile=True, movieFile={"id": 1, "dateAdded": arrs.at(timedelta(minutes=30))})
    ]
    arrs.poll(timedelta(hours=1))
    arrs.queues["radarr"] = [_stuck_film(50)]
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))

    assert [line[line.rindex("(try ") :] for line in arrs.history()] == ["(try 1)", "(try 1)"]


def test_a_copy_that_downloads_and_then_leaves_the_queue_unimported_gives_back_no_try(arrs):
    for first, hours in ((10, 1), (30, 3), (50, 1)):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=hours))
        arrs.queues["sonarr"] = [_downloading(_stuck_episode(first + 5, 1))]
        arrs.poll(timedelta(minutes=5))
        arrs.queues["sonarr"] = []
        arrs.poll(timedelta(minutes=5))

    assert len(arrs.history()) == 2


def test_a_removal_whose_search_waits_is_counted_whatever_radarr_grabs_meanwhile(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.failing = {"POST"}
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.failing = set()
    arrs.queues["radarr"] = [_downloading(_stuck_film(42))]
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))
    for queue_id in (43, 44):
        arrs.queues["radarr"] = [_stuck_film(queue_id)]
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=3))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == [
        "/api/v3/queue/40",
        "/api/v3/queue/43",
    ]
    assert [line[line.rindex("(try ") :] for line in arrs.history()] == ["(try 1)", "(try 2)"]


def test_an_unmonitored_film_whose_search_failed_is_still_searched_for(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune(monitored=False)]
    arrs.poll()
    arrs.failing = {"POST"}
    arrs.poll(timedelta(hours=1))
    arrs.failing = set()
    arrs.changes()
    arrs.poll(timedelta(hours=1))

    assert [c[3] for c in arrs.changes() if c[0] == "POST"] == [
        {"name": "MoviesSearch", "movieIds": [9]}
    ]
    assert arrs.history()[0].endswith("(try 1)")


def test_a_removal_radarr_refuses_is_asked_again_only_after_a_longer_wait(arrs, caplog):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.failing_ids = {40: 400}
    arrs.poll()
    _hourly(arrs, 3)
    assert len([c for c in arrs.changes() if c[0] == "DELETE"]) == 1
    _hourly(arrs, 1)

    assert len([c for c in arrs.changes() if c[0] == "DELETE"]) == 1
    assert len([r for r in caplog.records if "radarr:9" in r.getMessage()]) == 2
    assert arrs.history() == []


def test_a_film_still_listed_after_its_removal_reads_finding_another_copy(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.keeping = True
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.poll(timedelta(hours=1))

    assert arrs.states() == {"Dune": "finding_copy"}


def _tried(
    arrs: Arrs,
    key: str,
    episodes: list[int] | None = None,
    *,
    ago: timedelta = timedelta(hours=1),
    tries: int = 1,
) -> None:
    """A row as a try of *key* *ago* leaves it once its search has gone."""
    arrs.conn.execute(
        "INSERT INTO arr_import_retries (key, tries, tried_at, tried_episodes) VALUES (?, ?, ?, ?)",
        (key, tries, (arrs.now - ago).isoformat(), json.dumps(episodes or [])),
    )
    arrs.conn.commit()


def _waiting(arrs: Arrs, movie_id: int) -> None:
    """A removal of film *movie_id* an hour ago whose search is still to send."""
    at = (arrs.now - timedelta(hours=1)).isoformat()
    search = json.dumps({"name": "MoviesSearch", "movieIds": [movie_id]})
    arrs.conn.execute(
        "INSERT INTO arr_import_retries (key, tries, retried_at, tried_at, search, shown)"
        " VALUES (?, 1, ?, ?, ?, ?)",
        (f"radarr:{movie_id}", at, at, search, json.dumps({"title": "Dune"})),
    )
    arrs.conn.commit()


def _searched(arrs: Arrs) -> list[list[int]]:
    return [c[3]["movieIds"] for c in arrs.changes() if c[0] == "POST" and "movieIds" in c[3]]


def _rows(arrs: Arrs) -> set[str]:
    return {key for (key,) in arrs.conn.execute("SELECT key FROM arr_import_retries")}


def test_a_film_deleted_from_radarr_leaves_no_row_behind(arrs):
    arrs.movies = [_dune()]
    _tried(arrs, "radarr:9")
    _tried(arrs, "radarr:77")
    arrs.poll()

    assert _rows(arrs) == {"radarr:9"}


def test_a_radarr_answering_404_for_every_film_keeps_the_tries(arrs):
    _tried(arrs, "radarr:9")
    _tried(arrs, "radarr:77")
    arrs.poll()

    assert _rows(arrs) == {"radarr:9", "radarr:77"}


def _not_an_upgrade(queue_id: int) -> dict:
    message = "Not an upgrade for existing movie file"
    return _stuck_film(queue_id, statusMessages=[{"title": "Dune", "messages": [message]}])


def test_a_film_that_had_its_file_before_its_tries_keeps_them(arrs):
    arrs.movies = [_dune(hasFile=True, movieFile={"id": 1, "dateAdded": "2025-06-01T00:00:00Z"})]
    for queue_id in (40, 41, 42, 43):
        arrs.queues["radarr"] = [_not_an_upgrade(queue_id)]
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=1))
        _hourly(arrs, 3)

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == [
        "/api/v3/queue/40",
        "/api/v3/queue/41",
    ]


def test_a_season_that_had_its_files_before_its_tries_keeps_them(arrs):
    arrs.files = {501: "2025-06-01T00:00:00Z", 502: "2025-06-01T00:00:00Z"}
    for first in (10, 30, 50, 70):
        _season_stuck(arrs, first, download=f"pack-{first}")
        arrs.poll(timedelta(minutes=5))
        arrs.poll(timedelta(hours=1))
        _hourly(arrs, 3)
        arrs.queues["sonarr"] = []

    assert len(arrs.history()) == 2


def test_an_episode_imported_while_another_of_its_season_downloads_gives_the_tries_back(arrs):
    arrs.queues["sonarr"] = [_stuck_episode(10, 1, download="a")]
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.queues["sonarr"] = [_downloading(_stuck_episode(20, 2, download="b"))]
    arrs.poll(timedelta(minutes=30))
    arrs.files = {501: arrs.at(timedelta(minutes=30))}
    arrs.poll(timedelta(hours=1))
    arrs.queues["sonarr"] = [_stuck_episode(20, 2, download="b")]
    arrs.poll(timedelta(minutes=5))
    arrs.poll(timedelta(hours=1))

    assert [line[line.rindex("(try ") :] for line in arrs.history()] == ["(try 1)", "(try 1)"]


def test_a_film_imported_while_its_search_waits_still_gets_the_search(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.poll()
    arrs.failing = {"POST"}
    arrs.poll(timedelta(hours=1))
    arrs.failing = set()
    arrs.movies = [
        _dune(hasFile=True, movieFile={"id": 1, "dateAdded": arrs.at(timedelta(minutes=30))})
    ]
    arrs.changes()
    arrs.poll(timedelta(hours=1))

    assert [c[3] for c in arrs.changes() if c[0] == "POST"] == [
        {"name": "MoviesSearch", "movieIds": [9]}
    ]
    assert arrs.history()[0].endswith("(try 1)")


def test_a_radarr_that_does_not_answer_is_asked_once_in_a_run(arrs):
    for movie_id in (9, 10, 11):
        _tried(arrs, f"radarr:{movie_id}")
    arrs.slow = {"radarr"}
    arrs.poll()

    assert len(arrs.reads) == 1
    assert len(_rows(arrs)) == 3


def test_a_show_is_read_once_for_all_its_tried_seasons(arrs):
    for season in (11, 12, 13):
        _tried(arrs, f"sonarr:5:{season}", [501])
    arrs.files = {501: "2025-06-01T00:00:00Z"}
    arrs.poll()

    assert arrs.reads.count(("sonarr", "/api/v3/series/5")) == 1


def test_a_search_sonarr_always_refuses_is_given_up_after_a_day(arrs, caplog):
    _season_stuck(arrs, 10)
    arrs.refusing = 400
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    _hourly(arrs, 30)
    told = len([r for r in caplog.records if "sonarr:5:13" in r.getMessage()])
    _hourly(arrs, 10)

    assert "finding_copy" not in arrs.states().values()
    assert len([r for r in caplog.records if "sonarr:5:13" in r.getMessage()]) == told <= 26


def test_a_film_whose_read_fails_holds_back_no_other_films_removal(arrs):
    _tried(arrs, "radarr:77")
    arrs.broken = {"/api/v3/movie/77"}
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == ["/api/v3/queue/40"]


def test_a_film_whose_read_fails_holds_back_no_waiting_search(arrs):
    _tried(arrs, "radarr:77")
    arrs.broken = {"/api/v3/movie/77"}
    _waiting(arrs, 9)
    arrs.poll()

    assert _searched(arrs) == [[9]]


def test_a_radarr_whose_reads_time_out_still_has_its_stuck_film_removed(arrs):
    _tried(arrs, "radarr:77")
    arrs.slow = {"radarr"}
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[2] for c in arrs.changes() if c[0] == "DELETE"] == ["/api/v3/queue/40"]


def test_a_tried_season_is_read_alone_and_not_the_whole_show(arrs):
    _tried(arrs, "sonarr:5:13", [501])
    arrs.files = {501: arrs.at(), 502: "2025-06-01T00:00:00Z"}
    arrs.poll()

    assert sorted(path for _service, path in arrs.reads) == [
        "/api/v3/episode?seriesId=5&seasonNumber=13",
        "/api/v3/episodefile/1501",
        "/api/v3/series/5",
    ]
    assert _rows(arrs) == set()


def test_a_waiting_search_that_gets_no_answer_holds_back_the_next_one(arrs):
    _waiting(arrs, 9)
    _waiting(arrs, 10)
    arrs.failing = {"POST"}
    arrs.poll()

    assert len({tuple(ids) for ids in _searched(arrs)}) == 1


def test_a_waiting_search_that_gets_no_answer_holds_back_removals(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.poll()
    _waiting(arrs, 10)
    arrs.failing = {"POST"}
    arrs.poll(timedelta(hours=1))

    assert [c for c in arrs.changes() if c[0] == "DELETE"] == []


def test_a_title_tried_a_month_ago_and_out_of_the_queue_is_forgotten(arrs):
    _tried(arrs, "radarr:9", ago=timedelta(days=31), tries=2)
    _tried(arrs, "radarr:10", ago=timedelta(days=29), tries=2)
    arrs.movies = [_dune(), _dune(id=10)]
    arrs.poll()

    assert _rows(arrs) == {"radarr:10"}


def test_a_copy_stuck_for_a_month_keeps_its_count_of_tries(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.keeping = True
    arrs.poll()
    for _ in range(4 * 40):
        arrs.poll(timedelta(hours=6))

    tries = [int(re.search(r"\(try (\d+)\)", line)[1]) for line in arrs.history()]
    assert len(tries) > 3
    assert tries == list(range(1, len(tries) + 1))
    assert "has not imported for" in arrs.history()[-1]


def test_a_row_stored_before_its_try_time_was_kept_gets_its_tries_back_on_an_import(
    arrs, tmp_path, monkeypatch
):
    monkeypatch.setattr("mediaman.db.connection.now_utc", lambda: arrs.now)
    arrs.conn.execute("INSERT INTO arr_import_retries (key, tries) VALUES ('radarr:9', 2)")
    arrs.conn.commit()
    arrs.conn.close()
    arrs.conn = init_db(str(tmp_path / "mediaman.db"))
    arrs.movies = [
        _dune(hasFile=True, movieFile={"id": 1, "dateAdded": arrs.at(timedelta(days=1))})
    ]
    arrs.poll(timedelta(days=2))

    assert _rows(arrs) == set()


def test_a_season_tried_before_its_episodes_were_kept_gets_its_tries_back_on_an_import(arrs):
    _tried(arrs, "sonarr:5:13")
    arrs.files = {502: arrs.at()}
    arrs.poll()

    assert _rows(arrs) == set()


def test_a_search_sonarr_will_not_take_the_key_for_is_kept_until_it_does(arrs):
    _season_stuck(arrs, 10)
    arrs.refusing = 401
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    _hourly(arrs, 30)
    arrs.refusing = 0
    arrs.poll(timedelta(hours=1))

    assert arrs.history()[-1].endswith("(try 1)")


def test_a_removal_radarr_will_not_take_the_key_for_spends_no_try(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.failing_ids = {40: 403}
    arrs.poll()
    _hourly(arrs, 3)
    arrs.failing_ids = {}
    arrs.poll(timedelta(hours=1))

    assert arrs.history() == [arrs.history()[0]]
    assert arrs.history()[0].endswith("(try 1)")


def test_a_film_whose_read_fails_keeps_no_other_film_from_getting_its_tries_back(arrs):
    _tried(arrs, "radarr:77")
    _tried(arrs, "radarr:9")
    arrs.broken = {"/api/v3/movie/77"}
    arrs.movies = [_dune(hasFile=True, movieFile={"id": 1, "dateAdded": arrs.at()})]
    arrs.poll()

    assert _rows(arrs) == {"radarr:77"}


def test_a_title_tried_a_month_ago_whose_copy_is_still_queued_keeps_its_tries(arrs):
    _tried(arrs, "radarr:9", ago=timedelta(days=31), tries=2)
    arrs.movies = [_dune()]
    arrs.queues["radarr"] = [
        _stuck_film(40, trackedDownloadStatus="ok", trackedDownloadState="importing")
    ]
    arrs.poll()
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.poll(timedelta(minutes=5))
    _hourly(arrs, 3)

    assert [c for c in arrs.changes() if c[0] == "DELETE"] == []


def test_a_season_whose_read_never_answers_holds_back_no_other_show_for_good(arrs):
    _tried(arrs, "sonarr:5:11")
    _tried(arrs, "sonarr:5:13", [501])
    arrs.hanging = {"/api/v3/episode?seriesId=5&seasonNumber=11"}
    arrs.files = {501: arrs.at()}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert _rows(arrs) == {"sonarr:5:11"}


_PACK_NOT_IMPORTED = "One or more episodes expected in this release were not imported or missing"


def _pack_held(arrs: Arrs, message: str) -> None:
    """Alone's season pack held as Sonarr holds one whose files each failed to import: a first
    message saying only that, then one for each file with what kept it out."""
    _season_stuck(arrs, 10)
    for record in arrs.queues["sonarr"]:
        record["statusMessages"] = [
            {"title": _PACK_NOT_IMPORTED + " from the release", "messages": []},
            {"title": "Alone.S13E01.mkv", "messages": [message]},
            {"title": "Alone.S13E02.mkv", "messages": [message]},
        ]


@pytest.mark.parametrize(
    "message",
    [
        "Not enough free space",
        "Access to the path '/tv/Alone/Season 13' is denied.",
        f"[/downloads/{'deep/' * 40}Alone.S13] is not a valid local path. You may need a Remote"
        " Path Mapping.",
    ],
)
def test_a_pack_held_by_sonarrs_setup_after_its_first_message_is_left_for_an_admin(arrs, message):
    _pack_held(arrs, message)
    arrs.poll()
    _hourly(arrs, 3)

    assert arrs.changes() == []
    assert arrs.states() == {"Alone": "needs_attention"}


@pytest.mark.parametrize(
    "message",
    [
        "Episode has a TBA title and recently aired",
        "Episode does not have a title and recently aired",
        "Locked file, try again later",
    ],
)
def test_a_download_sonarr_holds_for_a_while_is_waited_for_not_removed(arrs, message):
    _pack_held(arrs, message)
    arrs.poll()
    _hourly(arrs, 3)

    assert arrs.changes() == []
    assert arrs.states() == {"Alone": "almost_ready"}


@pytest.mark.parametrize(
    ("service", "version", "redownload", "ours"),
    [
        ("sonarr", "3.0.10.1567", True, False),
        ("sonarr", "3.0.10.1567", False, True),
        ("sonarr", "4.0.14.2939", True, True),
        ("sonarr", None, True, True),
        ("radarr", "4.7.1.7640", True, False),
        ("radarr", "4.7.2.7686", True, True),
        ("radarr", "5.26.2.10099", True, True),
    ],
)
def test_each_retry_is_searched_for_once_whichever_version_answers(
    arrs, service, version, redownload, ours
):
    arrs.versions[service] = version
    arrs.redownload = redownload
    if service == "sonarr":
        _season_stuck(arrs, 10)
    else:
        arrs.queues["radarr"] = [_stuck_film(40)]
        arrs.movies = [_dune()]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    searches = [c for c in arrs.changes() if c[0] == "POST"]
    assert len(searches) == (1 if ours else 0)
    assert len(searches) + len(arrs.own_searches) == 1
    assert arrs.history()[-1].endswith("(try 1)")
    assert arrs.states()[{"sonarr": "Alone", "radarr": "Dune"}[service]] == "finding_copy"


def test_an_older_sonarrs_version_is_read_once_by_the_one_client_that_makes_all_its_retries(arrs):
    arrs.versions["sonarr"] = "3.0.10.1567"
    arrs.keeping = True
    _season_stuck(arrs, 10)
    arrs.poll()
    _hourly(arrs, 80)

    assert [line[line.rindex("(try ") :] for line in arrs.history()][:2] == ["(try 1)", "(try 2)"]
    # One client is kept for the run here; the real builders make a new one per call, which
    # reads the version again each time.
    assert arrs.version_reads == 1


_OLDER = {"sonarr": "3.0.10.1567", "radarr": "4.7.1.7640"}


def _one_stuck(arrs: Arrs, service: str) -> int:
    arrs.versions[service] = _OLDER[service]
    if service == "sonarr":
        arrs.queues["sonarr"] = [_stuck_episode(10, 1)]
        return 10
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    return 40


@pytest.mark.parametrize("service", ["sonarr", "radarr"])
@pytest.mark.parametrize(
    ("removal", "ours"),
    [(None, 0), (404, 1), (requests.ReadTimeout("no answer"), 1)],
    ids=["removed", "already-gone", "answer-lost"],
)
def test_an_older_service_is_sent_the_search_unless_it_plainly_removed_the_download(
    arrs, service, removal, ours
):
    queue_id = _one_stuck(arrs, service)
    if removal is not None:
        arrs.failing_ids = {queue_id: removal}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert len([c for c in arrs.changes() if c[0] == "POST"]) == ours
    assert len(arrs.own_searches) + ours == 1
    assert arrs.history()[-1].endswith("(try 1)")


def test_an_older_sonarr_that_finds_one_of_two_downloads_gone_is_sent_the_search(arrs):
    arrs.versions["sonarr"] = _OLDER["sonarr"]
    _season_stuck(arrs, 10, download="a")
    arrs.queues["sonarr"][1]["downloadId"] = "b"
    arrs.failing_ids = {11: 404}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[3] for c in arrs.changes() if c[0] == "POST"] == [
        {"name": "EpisodeSearch", "episodeIds": [501, 502]}
    ]


def test_an_older_sonarr_that_finds_a_lower_id_gone_before_its_download_is_removed_is_not_searched(
    arrs,
):
    arrs.versions["sonarr"] = _OLDER["sonarr"]
    _season_stuck(arrs, 10, download="a")
    arrs.failing_ids = {10: 404}
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c for c in arrs.changes() if c[0] == "POST"] == []
    assert len(arrs.own_searches) == 1


# ── Mended where it lies, and every way a download sticks ──

_RELEASE = "Adventure.Time.2010.S02E03.Loyalty.to.the.King.1080p.EN-GR.BluRay.x264-Smad"
_MKV = b"\x1a\x45\xdf\xa3" + b"\0" * 60
_BY_ID = (
    "Found matching series via grab history, but release was matched to series by ID."
    " Automatic import is not possible. See the FAQ for details."
)


def _history_of(arrs: Arrs, action: str) -> list[str]:
    rows = arrs.conn.execute(
        "SELECT detail FROM audit_log WHERE action=? ORDER BY id", (action,)
    ).fetchall()
    return [r[0] for r in rows]


def _says(arrs: Arrs, message: str, **more) -> None:
    for record in arrs.queues["sonarr"]:
        record["statusMessages"] = [{"title": record["title"], "messages": [message]}]
        record.update(more)


@pytest.fixture
def obfuscated(arrs, tmp_path):
    """Alone S13's stuck pack, whose one file came with no extension, in a downloads folder
    mediaman is told of and can write to."""
    root = tmp_path / "completed"
    folder = root / "comptv" / _RELEASE
    folder.mkdir(parents=True)
    (folder / "a8f3c2e9b1").write_bytes(_MKV)
    insert_settings(arrs.conn, import_fix_folder=str(root))
    _season_stuck(arrs, 10)
    _says(
        arrs,
        f"No files found are eligible for import in {folder}",
        title=_RELEASE,
        outputPath=str(folder),
    )
    return folder


def test_an_obfuscated_download_is_renamed_and_imported_again_before_any_other_copy(
    arrs, obfuscated
):
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert sorted(p.name for p in obfuscated.iterdir()) == ["a8f3c2e9b1.mkv"]
    scan = {
        "name": "DownloadedEpisodesScan",
        "path": str(obfuscated),
        "importMode": "auto",
        "downloadClientId": "pack-1",
    }
    assert arrs.changes() == [("POST", "sonarr", "/api/v3/command", scan)]
    (line,) = _history_of(arrs, "import_renamed")
    assert line.startswith("'Alone' S13 couldn't be imported (No files found are eligible")
    assert line.endswith("; renamed 1 file so Sonarr could import it")
    assert arrs.history() == []


def _writes_held_at_commands(arrs, fake_http) -> list[bool]:
    """Whether the job's connection held a write open as each command reached the service."""
    held: list[bool] = []
    inner = arrs._answer

    def answer(method, url, **kwargs):
        if method == "POST" and urlsplit(url).path == "/api/v3/command":
            held.append(arrs.conn.in_transaction)
        return inner(method, url, **kwargs)

    fake_http.handler(answer)
    return held


def test_a_rename_in_place_holds_no_write_open_while_the_service_is_asked_nor_after(
    arrs, obfuscated, fake_http
):
    held = _writes_held_at_commands(arrs, fake_http)
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert held == [False]
    assert not arrs.conn.in_transaction


def test_a_rename_in_place_says_so_in_the_log_with_the_title_the_files_and_the_mode(
    arrs, obfuscated, caplog
):
    with caplog.at_level(logging.INFO, logger="mediaman.services.arr.import_retry"):
        arrs.poll()
        arrs.poll(timedelta(hours=1))
    (line,) = [r.getMessage() for r in caplog.records if "import_retry.renamed" in r.getMessage()]
    assert "title='Alone' S13" in line and "files=1" in line and "mode=no_files" in line
    assert str(obfuscated) not in line


def test_each_episode_downloaded_on_its_own_is_renamed_in_its_own_folder(arrs, tmp_path):
    root = tmp_path / "completed"
    insert_settings(arrs.conn, import_fix_folder=str(root))
    releases = ["Alone.S13E01.1080p-NTb", "Alone.S13E02.1080p-NTb"]
    arrs.queues["sonarr"] = []
    for n, release in enumerate(releases, start=1):
        folder = root / "comptv" / release
        folder.mkdir(parents=True)
        (folder / f"obf{n}").write_bytes(_MKV)
        arrs.queues["sonarr"].append(
            _stuck_episode(
                9 + n,
                n,
                download=f"nzb-{n}",
                title=release,
                outputPath=str(folder),
                statusMessages=[{"title": release, "messages": ["No files found are eligible"]}],
            )
        )
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[3]["downloadClientId"] for c in arrs.changes()] == ["nzb-1", "nzb-2"]
    assert sorted(p.name for p in root.glob("comptv/*/*")) == ["obf1.mkv", "obf2.mkv"]
    assert _history_of(arrs, "import_renamed")[0].endswith(
        "renamed 2 files so Sonarr could import them"
    )


def test_a_renamed_download_still_stuck_an_hour_on_is_blocklisted_and_searched_again(
    arrs, obfuscated
):
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.changes()
    arrs.poll(timedelta(minutes=30))
    assert arrs.changes() == []
    arrs.poll(timedelta(minutes=30))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert arrs.history()[0].endswith("(try 1)")


def test_with_no_downloads_folder_set_an_obfuscated_download_is_replaced_at_once(arrs, obfuscated):
    arrs.conn.execute("UPDATE settings SET value='' WHERE key='import_fix_folder'")
    arrs.conn.commit()
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert sorted(p.name for p in obfuscated.iterdir()) == ["a8f3c2e9b1"]


def test_a_download_whose_folder_is_out_of_the_downloads_folder_is_never_renamed(
    arrs, obfuscated, tmp_path
):
    elsewhere = tmp_path / "library" / _RELEASE
    elsewhere.mkdir(parents=True)
    (elsewhere / "film").write_bytes(_MKV)
    _says(arrs, "No files found are eligible for import", outputPath=str(elsewhere))
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert sorted(p.name for p in elsewhere.iterdir()) == ["film"]
    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]


def _manual(episode: int) -> dict:
    return {
        "path": f"/downloads/completed/comptv/Alone.S13/{episode}.mkv",
        "folderName": "Alone.S13",
        "series": {"id": 5},
        "seasonNumber": 13,
        "episodes": [{"id": 500 + episode}],
        "quality": {"quality": {"id": 7, "name": "WEBDL-1080p"}},
        "languages": [{"id": 1, "name": "English"}],
        "releaseGroup": "NTb",
        "downloadId": "pack-1",
        "indexerFlags": 0,
        "releaseType": "singleEpisode",
        "rejections": [],
    }


def test_a_release_matched_by_id_alone_is_imported_by_hand_when_each_file_is_beyond_doubt(arrs):
    _season_stuck(arrs, 10)
    _says(arrs, _BY_ID, outputPath="/downloads/completed/comptv/Alone.S13")
    arrs.manual = [_manual(1), _manual(2)]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    (made,) = arrs.changes()
    assert made[:3] == ("POST", "sonarr", "/api/v3/command")
    assert made[3]["name"] == "ManualImport"
    assert [(f["seriesId"], f["episodeIds"]) for f in made[3]["files"]] == [(5, [501]), (5, [502])]
    assert arrs.manual_asked == [
        {"downloadId": ["pack-1"], "seriesId": ["5"], "filterExistingFiles": ["true"]}
    ]
    assert _history_of(arrs, "import_imported")[0].endswith(
        "imported its 2 files by hand in Sonarr"
    )


def test_an_import_by_hand_leaves_no_write_open_for_other_writers_to_wait_on(arrs):
    _season_stuck(arrs, 10)
    _says(arrs, _BY_ID, outputPath="/downloads/completed/comptv/Alone.S13")
    arrs.manual = [_manual(1), _manual(2)]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert _history_of(arrs, "import_imported")
    assert not arrs.conn.in_transaction


def test_a_release_matched_by_id_whose_files_are_in_doubt_is_replaced(arrs):
    _season_stuck(arrs, 10)
    _says(arrs, _BY_ID, outputPath="/downloads/completed/comptv/Alone.S13")
    arrs.manual = [{**_manual(1), "episodes": []}]
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert _history_of(arrs, "import_imported") == []


@pytest.mark.parametrize(
    "message",
    [
        "Sample",
        "Single episode file contains all episodes in seasons. Review file name or manually import",
        "Episode wasn't in the release",
    ],
)
def test_a_release_no_copy_of_it_mends_is_blocklisted_and_searched_again(arrs, message):
    _season_stuck(arrs, 10)
    _says(arrs, message)
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert f"({message})" in arrs.history()[0]


def test_a_season_given_up_on_after_two_tries_before_is_tried_again(arrs):
    """What an older mediaman left: two tries spent, stuck since, never acted on again."""
    _season_stuck(arrs, 10)
    _says(arrs, "Single episode file contains all episodes in seasons.")
    since = (arrs.now - timedelta(days=3)).isoformat()
    arrs.conn.execute(
        "INSERT INTO arr_import_retries (key, stuck_since, tries, tried_at)"
        " VALUES ('sonarr:5:13', ?, 2, ?)",
        (since, since),
    )
    arrs.conn.commit()
    arrs.poll()

    assert [c[0] for c in arrs.changes()] == ["DELETE", "DELETE", "POST"]
    assert arrs.history()[0].endswith("(try 3)")


def test_a_title_stuck_for_a_week_says_so_in_history_on_each_daily_try(arrs):
    arrs.queues["radarr"] = [_stuck_film(40)]
    arrs.movies = [_dune()]
    arrs.keeping = True
    arrs.poll()
    for _ in range(4 * 12):
        arrs.poll(timedelta(hours=6))

    lines = arrs.history()
    assert "has not imported for" not in lines[0]
    assert re.search(r"It has not imported for \d+ days; mediaman keeps trying", lines[-1])


def _paused(arrs: Arrs) -> None:
    arrs.queues["sonarr"] = [
        _stuck_episode(
            9 + n,
            n,
            status="paused",
            trackedDownloadState="downloading",
            trackedDownloadStatus="ok",
            statusMessages=[],
            sizeleft=600,
        )
        for n in (1, 2)
    ]


def test_a_paused_download_is_waited_for_as_someone_paused_it(arrs):
    _paused(arrs)
    arrs.poll()
    arrs.poll(timedelta(hours=30))

    assert arrs.changes() == []


def test_a_held_download_whose_client_sonarr_cannot_reach_is_never_touched(arrs):
    _season_stuck(arrs, 10)
    for record in arrs.queues["sonarr"]:
        record["status"] = "downloadClientUnavailable"
    arrs.poll()
    for _ in range(3):
        arrs.poll(timedelta(days=1))

    assert arrs.changes() == []
    assert arrs.history() == []


def test_a_stalled_download_is_replaced_once_it_has_gone_nowhere_for_hours(arrs):
    arrs.queues["sonarr"] = [
        _stuck_episode(
            10,
            1,
            status="warning",
            trackedDownloadState="downloading",
            statusMessages=[
                {
                    "title": "Alone.S13.1080p",
                    "messages": ["The download is stalled with no connections"],
                }
            ],
            sizeleft=600,
        )
    ]
    arrs.poll()
    arrs.poll(timedelta(hours=5))
    assert arrs.changes() == []
    arrs.poll(timedelta(hours=1))

    assert [c[0] for c in arrs.changes()] == ["DELETE", "POST"]


def test_each_kind_of_mend_is_listed_under_downloads_in_history_for_an_admin(arrs, obfuscated):
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    arrs.poll(timedelta(hours=1))
    viewer = history.Viewer(1, "admin", "2020-01-01T00:00:00Z", True)
    page = history.history_page(arrs.conn, viewer, history.Filters(action="downloads"))

    assert {e.action for e in page.entries} == {"import_renamed", "import_retried"}


def _old_films(arrs: Arrs, count: int) -> list[int]:
    """*count* films stuck for days that an older mediaman gave up on, the first stuck longest."""
    ids = list(range(20, 20 + count))
    arrs.queues["radarr"] = [
        _stuck_film(
            100 + movie_id,
            movieId=movie_id,
            movie={"id": movie_id, "title": f"Film {movie_id}", "tmdbId": movie_id, "images": []},
        )
        for movie_id in ids
    ]
    arrs.movies = [
        _dune(id=movie_id, title=f"Film {movie_id}", tmdbId=movie_id) for movie_id in ids
    ]
    for n, movie_id in enumerate(ids):
        since = (arrs.now - timedelta(days=10 - n)).isoformat()
        arrs.conn.execute(
            "INSERT INTO arr_import_retries (key, stuck_since, tries, first_stuck_at)"
            " VALUES (?, ?, 2, ?)",
            (f"radarr:{movie_id}", since, since),
        )
    arrs.conn.commit()
    return ids


def test_no_more_than_two_downloads_of_a_service_are_replaced_in_a_run_oldest_first(arrs):
    ids = _old_films(arrs, 6)
    arrs.poll()
    first = [c[2] for c in arrs.changes() if c[0] == "DELETE"]
    arrs.poll(timedelta(hours=1))
    second = [c[2] for c in arrs.changes() if c[0] == "DELETE"]

    assert first == [f"/api/v3/queue/{100 + i}" for i in ids[:2]]
    assert second == [f"/api/v3/queue/{100 + i}" for i in ids[2:4]]


def _matched_by_id(arrs: Arrs, *, held: bool = True) -> None:
    _season_stuck(arrs, 10)
    _says(arrs, _BY_ID, outputPath="/downloads/completed/comptv/Alone.S13")
    if not held:
        for record in arrs.queues["sonarr"]:
            record["trackedDownloadStatus"] = "ok"
    arrs.manual = [_manual(1), _manual(2)]


def _manual_imports(made: list) -> int:
    return len([c for c in made if c[0] == "POST" and c[3].get("name") == "ManualImport"])


def test_a_download_that_comes_and_goes_is_imported_by_hand_once_and_then_replaced(arrs):
    made = []
    for held in (True, True, False, True, True):
        _matched_by_id(arrs, held=held)
        arrs.poll(timedelta(hours=1))
        made += arrs.changes()

    assert _manual_imports(made) == 1
    assert len(arrs.manual_asked) == 1, "the files are read once for the one import by hand"
    assert [c[2] for c in made if c[0] == "DELETE"] == ["/api/v3/queue/10", "/api/v3/queue/11"]


def test_an_import_by_hand_that_may_have_gone_through_ends_the_run_for_that_title(arrs):
    _matched_by_id(arrs)
    arrs.breaking["ManualImport"] = requests.ReadTimeout("no answer")
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    made = arrs.changes()
    assert _manual_imports(made) == 1
    assert [c for c in made if c[0] == "DELETE"] == []
    arrs.breaking = {}
    arrs.poll(timedelta(hours=1))

    made = arrs.changes()
    assert _manual_imports(made) == 0, "one that may have gone through is not sent twice"
    assert len([c for c in made if c[0] == "DELETE"]) == 2


def test_a_season_beside_a_download_whose_client_is_unreachable_is_left_as_it_is(arrs):
    _season_stuck(arrs, 10)
    _says(arrs, "Sample")
    arrs.queues["sonarr"][0]["status"] = "downloadClientUnavailable"
    arrs.poll()
    arrs.poll(timedelta(hours=3))

    assert arrs.changes() == []


def test_an_import_by_hand_that_could_not_be_sent_is_sent_again_next_run(arrs):
    _matched_by_id(arrs)
    arrs.breaking["ManualImport"] = requests.ConnectionError("refused")
    arrs.poll()
    arrs.poll(timedelta(hours=1))
    assert [c for c in arrs.changes() if c[0] == "DELETE"] == []
    arrs.breaking = {}
    arrs.poll(timedelta(hours=1))

    made = arrs.changes()
    assert _manual_imports(made) == 1
    assert [c for c in made if c[0] == "DELETE"] == []


def test_a_title_is_mended_in_place_by_one_run_alone(arrs):
    _season_stuck(arrs, 10)
    arrs.poll()
    (since,) = arrs.conn.execute(
        "SELECT stuck_since FROM arr_import_retries WHERE key='sonarr:5:13'"
    ).fetchone()

    assert _mark_fixed(arrs.conn, "sonarr:5:13", since, arrs.now)
    assert not _mark_fixed(arrs.conn, "sonarr:5:13", since, arrs.now)


def test_files_of_a_download_stuck_for_another_reason_are_never_renamed(arrs, obfuscated):
    _says(arrs, "Sample", title=_RELEASE, outputPath=str(obfuscated))
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert sorted(p.name for p in obfuscated.iterdir()) == ["a8f3c2e9b1"]


def _words(arrs: Arrs) -> str:
    reasons = [r["statusMessages"][0]["messages"][0] for r in arrs.queues["sonarr"]]
    return fixing_words(arrs.conn, "sonarr:5:", reasons)


def test_the_card_says_files_are_renamed_only_of_a_download_in_the_downloads_folder(
    arrs, obfuscated, tmp_path
):
    arrs.poll()
    assert _words(arrs) == "Fixing — renaming files"

    elsewhere = tmp_path / "elsewhere" / _RELEASE
    elsewhere.mkdir(parents=True)
    _says(arrs, "No files found are eligible for import", outputPath=str(elsewhere))
    arrs.poll(timedelta(minutes=5))
    assert _words(arrs) == "Fixing — finding another copy"


def test_the_card_says_another_copy_is_looked_for_once_a_rename_did_not_take(arrs, obfuscated):
    arrs.poll()
    arrs.poll(timedelta(hours=1))

    assert _words(arrs) == "Fixing — finding another copy"


def test_the_card_of_a_download_whose_client_is_unreachable_claims_no_fix(arrs):
    _season_stuck(arrs, 10)
    for record in arrs.queues["sonarr"]:
        record["status"] = "downloadClientUnavailable"
    arrs.poll()

    assert _words(arrs) == ""
