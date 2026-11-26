"""What /downloads says when Radarr, Sonarr or NZBGet cannot be reached."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC, SONARR_SPEC
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.downloads.nzbget import NzbgetClient
from mediaman.web.routes.downloads import router as downloads_router

_DOWN = requests.ConnectionError("http://10.0.0.9:7878 refused")


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet, all up and idle."""
    _reset_previous_queue()
    radarr, sonarr, nzbget = MagicMock(), MagicMock(), MagicMock()
    for client in (radarr, sonarr, nzbget):
        client.get_queue.return_value = []
    radarr.get_movies.return_value = []
    sonarr.get_series.return_value = []
    nzbget.get_status.return_value = {}
    for target, client in (
        ("arr.build.build_radarr_from_db", radarr),
        ("arr.build.build_sonarr_from_db", sonarr),
        ("downloads.download_queue.build_nzbget_from_db", nzbget),
    ):
        monkeypatch.setattr(f"mediaman.services.{target}", lambda *a, client=client: client)
    # The library holds every title here, so what Sonarr tells of a show is what is said.
    monkeypatch.setattr("mediaman.web.routes.downloads._in_library", lambda *a: True)
    yield radarr, sonarr, nzbget
    _reset_previous_queue()


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub):
    app = app_factory(downloads_router, conn=conn, state_extras={"templates": templates_stub})
    return authed_client(app, conn)


def _get(client) -> dict:
    return client.get("/api/downloads").json()


def test_nothing_is_unreachable_while_all_answer(services, client):
    data = _get(client)
    assert (data["unreachable"], data["outage"], data["subtitle"]) == ([], "", "")


def test_every_service_down_is_named_without_internals(services, client):
    radarr, sonarr, nzbget = services
    radarr.get_queue.side_effect = sonarr.get_queue.side_effect = _DOWN
    nzbget.get_status.side_effect = _DOWN
    resp = client.get("/api/downloads")
    assert resp.json()["unreachable"] == ["Radarr", "Sonarr", "NZBGet"]
    assert "Radarr, Sonarr and NZBGet" in resp.json()["outage"]
    assert "10.0.0.9" not in resp.text


def test_a_list_radarr_gives_only_in_part_counts_as_unreachable(services, client):
    services[0].get_movies.side_effect = _DOWN
    assert _get(client)["unreachable"] == ["Radarr"]


@pytest.mark.parametrize("service", ["Radarr", "Sonarr", "NZBGet"])
@pytest.mark.parametrize("answer", [{"error": "x"}, ["x"], {"records": ["x"], "totalRecords": 1}])
def test_a_service_that_answers_with_the_wrong_shape_is_unreachable(
    client, monkeypatch, services, fake_http, fake_response, service, answer
):
    broken = {
        "Radarr": ("arr.build.build_radarr_from_db", ArrClient(RADARR_SPEC, "http://r", "k")),
        "Sonarr": ("arr.build.build_sonarr_from_db", ArrClient(SONARR_SPEC, "http://s", "k")),
        "NZBGet": (
            "downloads.download_queue.build_nzbget_from_db",
            NzbgetClient("http://n", "u", "p"),
        ),
    }
    target, real = broken[service]
    monkeypatch.setattr(f"mediaman.services.{target}", lambda *a: real)
    if service == "NZBGet":
        fake_http.queue("POST", fake_response(json_data={"result": {}}))
        answer = {"result": answer}
    fake_http.default(fake_response(json_data=answer))

    resp = client.get("/api/downloads")

    assert resp.status_code == 200
    assert resp.json()["unreachable"] == [service]


def test_the_subtitle_names_the_outage_beside_what_is_in_progress(services, client):
    radarr, _, nzbget = services
    radarr.get_queue.side_effect = _DOWN
    nzbget.get_queue.return_value = [
        {"NZBName": "Dune.2021", "Status": "DOWNLOADING", "FileSizeMB": 10, "RemainingSizeMB": 5}
    ]
    subtitle = _get(client)["subtitle"]
    assert "1 item in progress" in subtitle
    assert "Radarr" in subtitle


def test_abandon_during_an_outage_says_so_instead_of_not_found(services, client):
    services[0].get_queue.side_effect = _DOWN
    resp = client.post("/api/downloads/radarr%3ADune/abandon", json={})
    assert resp.status_code == 503
    assert "Radarr" in resp.json()["detail"]


# Added after any poll of these tests.
_NEW_FILE = {"id": 3, "dateAdded": "2999-01-01T00:00:00Z"}
_POSTER = "https://artworks.thetvdb.com/banners/posters/dune.jpg"


def _queued(title: str, release: str, **record: object) -> dict[str, object]:
    """A queue entry; *record* goes into its film and its show."""
    images = [{"coverType": "poster", "remoteUrl": _POSTER}]
    return {
        "title": release,
        "size": 100,
        "sizeleft": 40,
        "status": "downloading",
        "movie": {"title": title, "images": images, **record},
        "series": {"id": 7, "title": title, "images": images, **record},
        "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "One"},
    }


def _loading(name: str) -> dict[str, object]:
    return {"NZBName": name, "Status": "DOWNLOADING", "FileSizeMB": 10, "RemainingSizeMB": 5}


_SERVICES = pytest.mark.parametrize(("service", "name"), [(0, "radarr"), (1, "sonarr")])


@_SERVICES
def test_what_finishes_while_its_service_is_down_is_recorded_once_it_is_back(
    services, client, service, name
):
    arr, other, nzbget = services[service], services[1 - service], services[2]
    arr.get_queue.return_value = [_queued("Dune", "Dune.2021.1080p")]
    nzbget.get_queue.return_value = [_loading("Dune.2021.1080p")]
    _get(client)

    arr.get_queue.side_effect = _DOWN
    nzbget.get_queue.return_value = []
    other.get_queue.return_value = [_queued("Other", "Other")]
    assert _get(client)["recent"] == []
    _get(client)

    arr.get_queue.side_effect = None
    arr.get_queue.return_value = []
    arr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "hasFile": True, "movieFile": _NEW_FILE}
    ]
    arr.get_series.return_value = [
        {"id": 7, "title": "Dune", "statistics": {"episodeFileCount": 1}}
    ]
    arr.get_episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 3}
    ]
    arr.get_episode_files.return_value = [_NEW_FILE]
    assert [r["id"] for r in _get(client)["recent"]] == [f"{name}:Dune"]


def test_an_item_its_service_left_out_of_half_an_answer_is_not_taken_for_finished(services, client):
    radarr = services[0]
    radarr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "monitored": True, "isAvailable": True}
    ]
    _get(client)

    radarr.get_movies.side_effect = _DOWN
    _get(client)

    radarr.get_movies.side_effect = None
    radarr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "monitored": True, "hasFile": True, "movieFile": _NEW_FILE}
    ]
    assert [r["id"] for r in _get(client)["recent"]] == ["radarr:Dune"]


@_SERVICES
def test_a_page_first_loaded_while_a_service_is_down_keeps_the_names_and_art_of_its_downloads(
    services, client, service, name
):
    arr, nzbget = services[service], services[2]
    arr.get_queue.return_value = [_queued("Dune: Part One", "dune.p1.2021.1080p")]
    nzbget.get_queue.return_value = [_loading("dune.p1.2021.1080p"), _loading("Manual.2020")]
    before = _get(client)

    arr.get_queue.side_effect = _DOWN
    for _ in range(2):
        data = _get(client)
        items = [data["hero"], *data["queue"]]
        assert [(i["id"], i["title"], i["poster_url"]) for i in items] == [
            (f"{name}:Dune: Part One", "Dune: Part One", ""),
            ("Manual.2020", "Manual", ""),
        ]
        assert data["recent"] == []
    assert before["hero"]["title"] == "Dune: Part One"

    # what NZBGet no longer holds is not made up from memory
    nzbget.get_queue.return_value = [_loading("Manual.2020")]
    data = _get(client)
    assert (data["hero"]["title"], data["queue"]) == ("Manual", [])


@pytest.mark.parametrize(
    ("added", "recorded"),
    [
        ("2026-05-01T11:44:00Z", []),
        ("2026-05-01T11:46:00Z", ["radarr:Dune"]),
        ("2026-05-01T12:30:00Z", ["radarr:Dune"]),
    ],
)
def test_a_film_is_recorded_for_a_file_no_older_than_its_download(
    services, client, monkeypatch, added, recorded
):
    noon = datetime(2026, 5, 1, 12, tzinfo=UTC)
    clock = [noon]
    monkeypatch.setattr("mediaman.services.downloads.download_queue.now_utc", lambda: clock[0])
    radarr = services[0]
    radarr.get_queue.return_value = [_queued("Dune", "Dune.2021.2160p")]
    _get(client)
    clock[0] = noon + timedelta(hours=1)
    _get(client)
    radarr.get_queue.side_effect = _DOWN
    _get(client)

    clock[0] = noon + timedelta(hours=2)
    radarr.get_queue.side_effect = None
    radarr.get_queue.return_value = []
    radarr.get_movies.return_value = [
        {"id": 1, "title": "Dune", "hasFile": True, "movieFile": {"dateAdded": added}}
    ]
    assert [r["id"] for r in _get(client)["recent"]] == recorded


def test_a_film_seen_through_an_outage_is_still_told_from_another_of_its_title(services, client):
    radarr, _, nzbget = services
    nzbget.get_queue.return_value = [_loading("Dune.2021.1080p")]
    radarr.get_movies.return_value = [
        {"id": 1, "tmdbId": 10, "title": "Dune", "monitored": True, "isAvailable": True}
    ]
    _get(client)
    radarr.get_queue.side_effect = radarr.get_movies.side_effect = _DOWN
    assert _get(client)["hero"]["id"] == "radarr:Dune#10"

    radarr.get_queue.side_effect = radarr.get_movies.side_effect = None
    nzbget.get_queue.return_value = []
    radarr.get_movies.return_value = [
        {"id": 1, "tmdbId": 10, "title": "Dune", "hasFile": True, "movieFile": _NEW_FILE},
        {"id": 2, "tmdbId": 20, "title": "Dune", "monitored": False},
    ]
    assert [r["id"] for r in _get(client)["recent"]] == ["radarr:Dune#10"]


_DUNE = {"id": 1, "tmdbId": 10, "title": "Dune"}
_OTHER_DUNE = {"id": 2, "tmdbId": 20, "title": "Dune"}
_FILED = {
    "hasFile": True,
    "movieFile": _NEW_FILE,
    "statistics": {"episodeFileCount": 1},
}


def _library(client, *titles: dict[str, object]) -> None:
    client.get_movies.return_value = client.get_series.return_value = list(titles)


def _give_episode_a_file(client) -> None:
    client.get_episodes.side_effect = lambda series_id: [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": series_id == 1, "episodeFileId": 3}
    ]
    client.get_episode_files.return_value = [_NEW_FILE]


@_SERVICES
def test_what_is_searched_queued_and_filed_is_told_from_another_of_its_title(
    services, client, service, name
):
    arr = services[service]
    wanted = {**_DUNE, "monitored": True, "isAvailable": True}
    _library(arr, wanted)
    arr.get_episodes.return_value = []
    assert _get(client)["hero"]["state"] == "searching"

    arr.get_queue.return_value = [_queued("Dune", "Dune.2021", tmdbId=10)]
    assert _get(client)["hero"]["state"] == "downloading"

    arr.get_queue.return_value = []
    _library(arr, {**_DUNE, **_FILED}, {**_OTHER_DUNE, "monitored": False})
    _give_episode_a_file(arr)
    assert [r["id"] for r in _get(client)["recent"]] == [
        {"radarr": "radarr:Dune#10"}.get(name, f"{name}:Dune")
    ]


@_SERVICES
def test_a_queued_title_that_takes_the_place_of_another_is_not_taken_for_it(
    services, client, service, name
):
    arr = services[service]
    _library(arr, {**_OTHER_DUNE, "monitored": True, "isAvailable": True})
    arr.get_episodes.return_value = []
    _get(client)

    arr.get_queue.return_value = [_queued("Dune", "Dune.2021", tmdbId=10)]
    _library(arr)
    _get(client)

    arr.get_queue.return_value = []
    _library(arr, {**_DUNE, **_FILED}, {**_OTHER_DUNE, "monitored": False})
    _give_episode_a_file(arr)
    assert [r["id"] for r in _get(client)["recent"]] == [
        {"radarr": "radarr:Dune#10"}.get(name, f"{name}:Dune")
    ]


def test_a_film_that_takes_the_place_of_another_of_its_title_is_not_taken_for_it(services, client):
    radarr = services[0]
    old = {"id": 1, "tmdbId": 10, "title": "Dune", "monitored": True, "isAvailable": True}
    new = {**old, "id": 2, "tmdbId": 20}
    radarr.get_movies.return_value = [old]
    _get(client)

    # the queue is read, then the ids are not, nor whether the other arrived
    radarr.get_movies.side_effect = [[new], _DOWN, _DOWN]
    assert _get(client)["hero"]["arr_id"] == 2

    radarr.get_movies.side_effect = None
    radarr.get_movies.return_value = [
        {"id": 1, "tmdbId": 10, "title": "Dune", "monitored": False},
        {**new, "hasFile": True, "movieFile": _NEW_FILE},
    ]
    assert [r["id"] for r in _get(client)["recent"]] == ["radarr:Dune#20"]


def _show(title: str, *files: int, monitored: bool = True) -> dict[str, object]:
    """A show of eight-episode seasons, all aired, with *files* of each."""
    seasons = [
        {
            "seasonNumber": n,
            "monitored": monitored,
            "statistics": {
                "episodeFileCount": have,
                # Sonarr leaves out of it an episode that is neither wanted nor there.
                "episodeCount": 8 if monitored else have,
                "previousAiring": "2020-01-01T00:00:00Z",
            },
        }
        for n, have in enumerate(files, start=1)
    ]
    stats = {"episodeFileCount": sum(files)}
    return {"id": len(title), "title": title, "seasons": seasons, "statistics": stats}


def test_a_recent_show_lacking_an_aired_season_is_there_in_part_until_it_is_whole(
    services, client, conn, monkeypatch
):
    sonarr = services[1]
    clock = [1000.0]
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.time.monotonic", lambda: clock[0]
    )
    with conn:
        conn.executemany(
            "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, ?)",
            [
                ("sonarr:Half", "Half", "series"),
                ("sonarr:Whole", "Whole", "series"),
                ("radarr:Half", "Half", "movie"),
            ],
        )
    sonarr.get_series.return_value = [_show("Half", 8, 0), _show("Whole", 8, 8)]

    def readiness() -> dict[str, str]:
        return {r["id"]: r["readiness"] for r in _get(client)["recent"]}

    in_part = {"sonarr:Half": "part", "sonarr:Whole": "ready", "radarr:Half": "ready"}
    assert readiness() == in_part

    clock[0] += 3600
    sonarr.get_queue.side_effect = sonarr.get_series.side_effect = _DOWN
    assert readiness() == in_part, "an outage tells nothing new"

    sonarr.get_queue.side_effect = sonarr.get_series.side_effect = None
    sonarr.get_series.return_value = [_show("Half", 8, 8), _show("Whole", 8, 8)]
    assert set(readiness().values()) == {"ready"}


@pytest.mark.parametrize("monitored", [True, False])
def test_a_recent_show_is_there_in_part_only_by_seasons_still_wanted(
    services, client, conn, monitored
):
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type)"
            " VALUES ('sonarr:Half', 'Half', 'series')"
        )
    services[1].get_series.return_value = [_show("Half", 8, 0, monitored=monitored)]
    expected = "part" if monitored else "ready"
    assert [r["readiness"] for r in _get(client)["recent"]] == [expected]


def test_a_recent_show_lacking_one_episode_wanted_of_a_skipped_season_is_there_in_part(
    services, client, conn
):
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type)"
            " VALUES ('sonarr:Half', 'Half', 'series')"
        )
    show = _show("Half", 8, 0, monitored=False)
    show["seasons"][1]["statistics"]["episodeCount"] = 1
    services[1].get_series.return_value = [show]
    assert [r["readiness"] for r in _get(client)["recent"]] == ["part"]


def test_a_recent_show_is_not_called_ready_before_sonarr_has_told(
    services, client, conn, monkeypatch
):
    sonarr = services[1]
    clock = [1000.0]
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.time.monotonic", lambda: clock[0]
    )
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type, poster_url)"
            " VALUES ('sonarr:Half', 'Half', 'series', ?)",
            (_POSTER,),
        )
    sonarr.get_queue.side_effect = _DOWN

    def readiness() -> list[str]:
        return [r["readiness"] for r in _get(client)["recent"]]

    assert readiness() == ["unknown"]

    # what the queue takes answers, what the shows are read from does not
    sonarr.get_queue.side_effect = None
    sonarr.get_series.side_effect = [[], _DOWN, [], [_show("Half", 8, 0)]]
    assert readiness() == ["unknown"]
    assert readiness() == ["unknown"]
    assert sonarr.get_series.call_count == 3, "a read that failed is not asked again at once"

    clock[0] += 31
    sonarr.get_series.side_effect = None
    sonarr.get_series.return_value = [_show("Half", 8, 0)]
    assert readiness() == ["part"]


def test_a_recent_show_added_since_sonarr_last_told_is_not_called_ready(
    services, client, conn, monkeypatch
):
    sonarr = services[1]
    clock = [1000.0]
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.time.monotonic", lambda: clock[0]
    )
    insert = "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES (?, ?, 'series')"
    with conn:
        conn.execute(insert, ("sonarr:Whole", "Whole"))
    sonarr.get_series.return_value = [_show("Whole", 8)]
    assert [r["readiness"] for r in _get(client)["recent"]] == ["ready"]

    with conn:
        conn.execute(insert, ("sonarr:New", "New"))
    clock[0] += 31
    sonarr.get_queue.side_effect = sonarr.get_series.side_effect = _DOWN
    told = {r["id"]: r["readiness"] for r in _get(client)["recent"]}
    assert told == {"sonarr:Whole": "ready", "sonarr:New": "unknown"}


@pytest.mark.parametrize("shows", [[_show("Gone", 0, 0)], [_show("Gone", 8, 0), _show("Gone", 8)]])
def test_a_recent_show_with_no_file_or_two_verdicts_claims_no_episodes(
    services, client, conn, shows
):
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type)"
            " VALUES ('sonarr:Gone', 'Gone', 'series')"
        )
    services[1].get_series.return_value = shows
    assert [r["readiness"] for r in _get(client)["recent"]] == ["unknown"]


def test_a_recent_show_that_is_not_sonarrs_is_not_asked_of_sonarr(services, client, conn):
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type)"
            " VALUES ('nzb:Some.Show.S01', 'Some Show', 'series')"
        )

    services[1].get_series.return_value = [_show("Some Show", 8, 0)]

    assert [r["readiness"] for r in _get(client)["recent"]] == ["ready"]


def test_a_read_of_the_shows_that_failed_is_asked_again_sooner_than_one_that_answered(
    services, client, conn, monkeypatch
):
    sonarr = services[1]
    clock = [1000.0]
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.time.monotonic", lambda: clock[0]
    )
    with conn:
        conn.execute(
            "INSERT INTO recent_downloads (dl_id, title, media_type, poster_url)"
            " VALUES ('sonarr:Half', 'Half', 'series', ?)",
            (_POSTER,),
        )

    def readiness() -> str:
        return _get(client)["recent"][0]["readiness"]

    # Each request reads the queue's shows first, then, when it is due, the recent ones.
    sonarr.get_series.side_effect = [[], _DOWN, [], [_show("Half", 8, 0)]]
    assert readiness() == "unknown"
    clock[0] += 6
    assert readiness() == "part", "a failed read is not held for the half minute"

    sonarr.get_series.side_effect = [[], [_show("Half", 8, 8)]]
    clock[0] += 20
    assert readiness() == "part", "one that answered is"
