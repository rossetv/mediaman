"""The half-hourly look at Radarr and Sonarr, against stub services: what it tells Activity, the
commands it sends and the caps that keep it from looping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import requests

from mediaman.services.arr import self_care
from mediaman.services.arr.self_care import care_lines, look_after_arrs, unmonitored_series

NOON = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GB = 1000**3


class StubArr:
    """Answers what a test sets; every command sent is kept."""

    def __init__(self) -> None:
        self.health: list[dict] = []
        self.roots: list[dict] = []
        self.disks: list[dict] = []
        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.movies: list[dict] = []
        self.series: list[dict] = []
        self.sent: list[dict] = []
        self.down: set[str] = set()

    def _answer(self, path: str, value):
        if any(path.startswith(p) for p in self.down):
            raise requests.ConnectionError(path)
        return value

    def _get_list(self, path: str):
        lists = {"/api/v3/health": self.health, "/api/v3/rootfolder": self.roots}
        return self._answer(path, lists.get(path, self.disks))

    def _get_page(self, path: str):
        return self._answer(path, (self.history, len(self.history)))

    def get_queue(self):
        return self._answer("/api/v3/queue", self.queue)

    def get_movies(self):
        return self._answer("/api/v3/movie", self.movies)

    def get_series(self):
        return self._answer("/api/v3/series", self.series)

    def send_command(self, command):
        self.sent.append(dict(command))


@pytest.fixture
def arrs(monkeypatch):
    sonarr, radarr = StubArr(), StubArr()
    monkeypatch.setattr(self_care, "build_sonarr_from_db", lambda _c, _k: sonarr)
    monkeypatch.setattr(self_care, "build_radarr_from_db", lambda _c, _k: radarr)
    return sonarr, radarr


def run(conn, at: datetime) -> list[str]:
    look_after_arrs(conn, "k", now=at)
    return [c["line"] for c in care_lines(conn)]


def test_a_health_warning_is_told_from_its_second_poll_and_cleared_when_it_goes(conn, arrs):
    sonarr, _ = arrs
    sonarr.health = [
        {
            "source": "IndexerStatusCheck",
            "type": "error",
            "message": "All indexers are unavailable",
        },
        {"source": "IndexerStatusCheck", "type": "error", "message": "Again"},
        {"source": "UpdateCheck", "type": "notice", "message": "An update"},
    ]
    assert run(conn, NOON) == []
    said = run(conn, NOON + timedelta(minutes=30))
    assert said == ["Sonarr can't reach any indexer, so nothing new will download."]
    assert care_lines(conn)[0]["needs_you"] is True
    sonarr.health = []
    assert run(conn, NOON + timedelta(hours=1)) == []
    # Back again, it waits a poll once more.
    sonarr.health = [{"source": "IndexerStatusCheck", "type": "error", "message": "x"}]
    assert run(conn, NOON + timedelta(hours=2)) == []


def test_a_read_that_fails_leaves_what_was_told(conn, arrs):
    sonarr, _ = arrs
    sonarr.health = [{"source": "RootFolderCheck", "type": "warning", "message": "Missing /tv"}]
    run(conn, NOON)
    run(conn, NOON + timedelta(minutes=30))
    sonarr.down = {"/api/v3/health"}
    assert run(conn, NOON + timedelta(hours=1)) == [
        "Sonarr can't find a library folder, so new episodes can't be imported."
    ]


def test_a_root_folder_low_on_space_is_told_by_size_or_share(conn, arrs):
    sonarr, radarr = arrs
    sonarr.roots = [{"path": "/tv", "freeSpace": 6 * GB}]
    sonarr.disks = [{"path": "/", "freeSpace": 6 * GB, "totalSpace": 20 * GB}]
    radarr.roots = [
        {"path": "/films/a", "freeSpace": 40 * GB},
        {"path": "/films/b", "freeSpace": 400 * GB},
    ]
    radarr.disks = [
        {"path": "/", "freeSpace": 1, "totalSpace": 1},
        {"path": "/films/a", "freeSpace": 40 * GB, "totalSpace": 1000 * GB},
        {"path": "/films/b", "freeSpace": 400 * GB, "totalSpace": 1000 * GB},
    ]
    assert run(conn, NOON) == [
        "Your TV folder has 6 GB left; new episodes will stop importing soon.",
        "Your film folder /films/a has 40 GB left; new films will stop importing soon.",
    ]


def _blocked(
    state: str = "importBlocked", message: str = "Access to the path '/tv/Show' is denied."
) -> dict:
    return {
        "status": "completed",
        "trackedDownloadState": state,
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "x", "messages": [message]}] if message else [],
        "downloadId": "ABC",
        "outputPath": "/downloads/Show.S01",
        "series": {"title": "Show"},
    }


def test_a_setup_fault_is_told_after_an_hour_and_scanned_at_most_three_times_once_cleared(
    conn, arrs
):
    sonarr, _ = arrs
    sonarr.queue = [_blocked()]
    assert run(conn, NOON) == []
    assert run(conn, NOON + timedelta(hours=1)) == [
        "Sonarr can't move Show into your library: no permission on /tv/Show."
    ]
    assert sonarr.sent == []
    sonarr.queue = [_blocked(state="importPending", message="")]
    for n in range(1, 6):
        run(conn, NOON + timedelta(hours=1, minutes=30 * n))
    assert (
        sonarr.sent
        == [
            {
                "name": "DownloadedEpisodesScan",
                "path": "/downloads/Show.S01",
                "downloadClientId": "ABC",
                "importMode": "Auto",
            }
        ]
        * 3
    )
    # Imported at last, it is forgotten.
    sonarr.queue = []
    run(conn, NOON + timedelta(hours=5))
    assert conn.execute("SELECT COUNT(*) FROM arr_care").fetchone()[0] == 0


def test_a_fault_that_clears_before_it_was_told_is_left_to_the_service(conn, arrs):
    sonarr, _ = arrs
    sonarr.queue = [_blocked()]
    run(conn, NOON)
    sonarr.queue = [_blocked(state="importPending", message="")]
    run(conn, NOON + timedelta(minutes=30))
    assert sonarr.sent == []


def test_an_unreachable_download_client_is_told_once_after_an_hour(conn, arrs):
    sonarr, _ = arrs
    sonarr.queue = [{"status": "downloadClientUnavailable", "downloadId": "A"}]
    sonarr.health = [
        {"source": "DownloadClientCheck", "type": "error", "message": "Unable to communicate"}
    ]
    assert run(conn, NOON) == []
    assert run(conn, NOON + timedelta(minutes=30)) == []
    assert run(conn, NOON + timedelta(hours=1)) == [
        "Sonarr can't reach the download client; downloads are on hold."
    ]


def test_a_title_failing_more_than_five_times_in_a_day_is_told(conn, arrs):
    sonarr, _ = arrs

    def failed(series_id, hours_ago, message="Unpacking failed"):
        return {
            "date": (NOON - timedelta(hours=hours_ago)).isoformat(),
            "seriesId": series_id,
            "series": {"title": f"Show {series_id}"},
            "data": {"message": message},
        }

    sonarr.history = [failed(1, h) for h in range(6)] + [failed(2, h) for h in range(5)]
    sonarr.history += [failed(2, 30), failed(2, 31)]  # yesterday's
    assert run(conn, NOON) == [
        "Show 1 downloads keep failing to unpack (6 today); Sonarr keeps trying other copies."
    ]


def _in_plex(conn, *, radarr_id=None, sonarr_id=None):
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, season_number, plex_library_id,"
        " plex_rating_key, added_at, file_path, file_size_bytes, radarr_id, sonarr_id)"
        " VALUES (?, 't', ?, ?, 1, 'k', '2026-01-01', '/x', 100, ?, ?)",
        (
            f"m{radarr_id}{sonarr_id}",
            "movie" if radarr_id else "season",
            None if radarr_id else 1,
            radarr_id,
            sonarr_id,
        ),
    )
    conn.commit()


def test_a_film_plex_has_but_radarr_misses_is_refreshed_daily_twice_then_left_to_an_admin(
    conn, arrs
):
    _, radarr = arrs
    _in_plex(conn, radarr_id=7)
    radarr.movies = [
        {"id": 7, "title": "Film", "monitored": True, "hasFile": False},
        {"id": 8, "title": "Other", "monitored": True, "hasFile": False},  # not in Plex
    ]
    run(conn, NOON)
    run(conn, NOON + timedelta(hours=12))
    assert radarr.sent == [{"name": "RefreshMovie", "movieIds": [7]}]
    for day in range(1, 5):
        said = run(conn, NOON + timedelta(days=day))
    assert radarr.sent == [{"name": "RefreshMovie", "movieIds": [7]}] * 2
    assert said == [
        "Plex has Film but Radarr still lists it as missing; check where Radarr looks for it."
    ]
    assert care_lines(conn)[0]["needs_you"] is True


def test_a_show_plex_has_but_sonarr_misses_is_rescanned_and_unmonitored_ones_are_noted(conn, arrs):
    sonarr, _ = arrs
    _in_plex(conn, sonarr_id=3)
    sonarr.series = [
        {
            "id": 3,
            "title": "Show",
            "monitored": True,
            "statistics": {"episodeFileCount": 0, "episodeCount": 8},
        },
        {"id": 4, "title": "Off", "monitored": False, "statistics": {}},
    ]
    said = run(conn, NOON)
    assert sonarr.sent == [{"name": "RescanSeries", "seriesId": 3}]
    assert said == [
        "Plex has Show but Sonarr listed it as missing; mediaman asked Sonarr to look again."
    ]
    assert unmonitored_series(conn) == {4}
    sonarr.series[1]["monitored"] = True
    run(conn, NOON + timedelta(minutes=30))
    assert unmonitored_series(conn) == set()


def test_one_pass_sends_a_service_no_more_than_its_budget(conn, arrs):
    _, radarr = arrs
    for movie_id in range(1, 9):
        _in_plex(conn, radarr_id=movie_id)
        radarr.movies.append({"id": movie_id, "title": "F", "monitored": True, "hasFile": False})
    run(conn, NOON)
    assert len(radarr.sent) == self_care.COMMANDS_PER_PASS


def test_a_service_no_longer_set_up_has_its_lines_cleared(conn, arrs, monkeypatch):
    sonarr, _ = arrs
    sonarr.roots = [{"path": "/tv", "freeSpace": GB}]
    assert run(conn, NOON)
    monkeypatch.setattr(self_care, "build_sonarr_from_db", lambda _c, _k: None)
    assert run(conn, NOON + timedelta(minutes=30)) == []


def test_a_card_of_a_show_sonarr_does_not_monitor_says_so(conn, arrs):
    from mediaman.services.downloads.download_queue import UNMONITORED_HINT, _mark_unmonitored

    sonarr, _ = arrs
    sonarr.series = [{"id": 4, "title": "Off", "monitored": False}]
    run(conn, NOON)
    searching = {
        "id": "sonarr:Off",
        "arr_id": 4,
        "state": "searching",
        "search_hint": "Searched 2×",
    }
    downloading = {"id": "sonarr:Off2", "arr_id": 4, "state": "downloading", "search_hint": ""}
    film = {"id": "radarr:Film", "arr_id": 4, "state": "searching", "search_hint": "Searched"}
    _mark_unmonitored(conn, [searching, downloading, film])
    assert searching["search_hint"] == UNMONITORED_HINT == "Not monitored in Sonarr"
    assert downloading["unmonitored"] is True and downloading["search_hint"] == ""
    assert "unmonitored" not in film
