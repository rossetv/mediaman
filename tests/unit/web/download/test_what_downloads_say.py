"""What Downloads, the downloading strip and the mailed page say of the same downloads."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.core.time import now_utc
from mediaman.services.arr import fetcher
from mediaman.services.downloads import strip
from mediaman.services.downloads.abandon import AbandonResult
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.infra import SafeHTTPError
from mediaman.services.watch_catalog import clear_library_index, invalidate
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.download.status import _radarr_status
from mediaman.web.routes.download.status._radarr import _radarr_queue_item
from mediaman.web.routes.download.status._sonarr import _sonarr_aggregate, _sonarr_queue_entries
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.downloads_strip import _with_titles
from mediaman.web.routes.downloads_strip import router as strip_router
from tests.helpers.factories import insert_media_item

_WAITING = "In your library soon"


def _movie(n: int, title: str) -> dict[str, object]:
    return {"id": n, "tmdbId": n, "title": title, "year": 2024, "monitored": True, "images": []}


def _clear() -> None:
    _reset_previous_queue()
    strip.shared.clear()
    strip.on_page.clear()
    clear_library_index()
    invalidate()


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet, all up and idle; *built* says which are set up."""
    _clear()
    radarr, sonarr, nzbget = MagicMock(), MagicMock(), MagicMock()
    for client in (radarr, sonarr, nzbget):
        client.get_queue.return_value = []
    radarr.get_movies.return_value = []
    sonarr.get_series.return_value = []
    nzbget.get_status.return_value = {}
    built = {"radarr": radarr, "sonarr": sonarr}
    for name in ("radarr", "sonarr"):
        monkeypatch.setattr(
            f"mediaman.services.arr.build.build_{name}_from_db",
            lambda *a, name=name: built[name],
        )
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda *a: nzbget
    )
    yield radarr, sonarr, nzbget, built
    _clear()


@pytest.fixture
def client(app_factory, conn, templates_stub):
    """An admin's session; no password, whose hashing takes a second."""
    from fastapi.testclient import TestClient

    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, created_at)"
        " VALUES ('admin', 'x', 'admin', '2026-01-01')"
    )
    conn.commit()
    app = app_factory(
        downloads_router, strip_router, conn=conn, state_extras={"templates": templates_stub}
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "admin"))
    return client


def _as_viewer(conn) -> None:
    conn.execute("UPDATE admin_users SET role = 'viewer'")
    conn.commit()


def _queued(status: str) -> dict[str, object]:
    """Radarr's entry of a film its download client holds but has not begun."""
    return {
        "movie": _movie(11, "Ember Coast"),
        "title": "Ember.Coast.2024.1080p",
        "status": status,
        "trackedDownloadState": "downloading",
        "size": 1000,
        "sizeleft": 1000,
        "timeleft": "00:05:00",
        "downloadId": "11",
    }


@pytest.mark.parametrize("status", ["queued", "paused"])
def test_a_download_waiting_its_turn_reads_queued_with_no_time_left_everywhere(
    services, client, status
):
    radarr, _, nzbget, _ = services
    radarr.get_queue.return_value = [_queued(status)]
    nzbget.get_status.return_value = {"DownloadRate": 1_000_000}
    nzbget.get_queue.return_value = [
        {
            "NZBName": "Ember.Coast.2024.1080p",
            "Status": status.upper(),
            "FileSizeMB": 100,
            "RemainingSizeMB": 100,
        }
    ]

    hero = client.get("/api/downloads").json()["hero"]
    (item,) = client.get("/api/downloads/strip").json()["items"]
    mailed = _radarr_queue_item([_queued(status)], 11)

    assert (hero["state"], hero["state_label"], hero["eta"]) == ("queued", "Queued", "")
    assert (item["state"], item["minutes_left"]) == ("queued", None)
    assert mailed is not None
    assert (mailed["state"], mailed["eta"]) == ("queued", "")


def test_a_refused_key_is_named_to_an_admin_and_not_taken_for_the_network(services, client):
    services[0].get_queue.side_effect = SafeHTTPError(401, "", "http://radarr:7878/api/v3/queue")
    services[2].get_status.side_effect = SafeHTTPError(403, "", "http://nzbget:6789/jsonrpc")
    services[1].get_queue.side_effect = requests.ConnectionError("down")

    data = client.get("/api/downloads").json()
    ring = client.get("/api/downloads/strip").json()

    assert data["refused"] == ["Radarr", "NZBGet"]
    assert data["outage"] == "Can't reach Sonarr or sign in to Radarr and NZBGet"
    assert ring["outage"] == data["outage"]


def test_a_viewer_is_told_only_that_downloads_are_not_available_while_a_key_is_refused(
    services, client, conn
):
    _as_viewer(conn)
    services[1].get_queue.side_effect = SafeHTTPError(403, "", "http://sonarr:8989/api/v3/queue")

    data = client.get("/api/downloads").json()
    ring = client.get("/api/downloads/strip").json()

    assert data["outage"] == ring["outage"] == "Downloads aren't available"
    assert data["strip"]["outage"] == "Downloads aren't available"
    assert "Sonarr" not in data["subtitle"]


def test_the_page_says_which_of_radarr_and_sonarr_are_set_up(services, client):
    services[3]["radarr"] = None

    page = client.get("/downloads").json()
    data = client.get("/api/downloads").json()

    assert page["downloads_set_up"] == data["downloads_set_up"] == {"movies": False, "shows": True}


def test_a_failed_stop_tracking_answers_its_words_as_the_message(services, client):
    services[0].get_queue.side_effect = requests.ConnectionError("down")

    resp = client.post("/api/downloads/radarr%3ADune/abandon", json={})

    assert resp.status_code == 503
    assert resp.json()["message"] == resp.json()["detail"]
    assert "Radarr" in resp.json()["message"]


def test_stop_tracking_that_radarr_refuses_says_so_and_rebuilds_no_queue(services, client):
    item = {"id": "radarr:Dune", "arr_id": 4, "kind": "movie", "state": "searching"}
    with (
        patch("mediaman.web.routes.downloads._lookup_dl_item", return_value=item),
        patch(
            "mediaman.web.routes.downloads.abandon_movie",
            return_value=AbandonResult(kind="movie", failed=[0], dl_id="radarr:Dune"),
        ),
    ):
        resp = client.post("/api/downloads/radarr%3ADune/abandon", json={"arr_id": 4})

    assert resp.status_code == 502
    assert "Radarr" in resp.json()["message"]
    assert "queue" not in resp.json()
    services[0].get_queue.assert_not_called()


@pytest.mark.parametrize(
    ("state", "said"),
    [
        ("searching", "Radarr didn't abandon it. Try again."),
        ("upcoming", "Radarr didn't stop tracking it. Try again."),
    ],
)
def test_a_failed_abandon_says_abandon_and_only_one_coming_soon_says_stop_tracking(
    services, client, state, said
):
    item = {"id": "radarr:Dune", "arr_id": 4, "kind": "movie", "state": state}
    with (
        patch("mediaman.web.routes.downloads._lookup_dl_item", return_value=item),
        patch(
            "mediaman.web.routes.downloads.abandon_movie",
            return_value=AbandonResult(kind="movie", failed=[0], dl_id="radarr:Dune"),
        ),
    ):
        resp = client.post("/api/downloads/radarr%3ADune/abandon", json={"arr_id": 4})

    assert resp.json()["message"] == said


def _hold_in_library(conn, tmdb_id: int, title: str) -> None:
    insert_media_item(conn, id=str(900 + tmdb_id), title=title, media_type="movie")
    conn.execute("UPDATE media_items SET tmdb_id = ? WHERE title = ?", (tmdb_id, title))
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()


def test_a_recent_film_is_ready_to_watch_only_once_the_library_holds_it(services, client, conn):
    _hold_in_library(conn, 12, "Dry Harbour")
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type) VALUES"
        " ('radarr:Ember Coast#11', 'Ember Coast', 'movie'),"
        " ('radarr:Dry Harbour#12', 'Dry Harbour', 'movie')"
    )
    conn.commit()

    recent = client.get("/api/downloads").json()["recent"]

    assert {r["title"]: r["readiness"] for r in recent} == {
        "Ember Coast": "unknown",
        "Dry Harbour": "ready",
    }


def test_the_strip_says_added_of_what_the_library_does_not_hold_yet(services, conn):
    _hold_in_library(conn, 12, "Dry Harbour")

    def ready(title: str, tmdb_id: int) -> strip.StripItem:
        return strip.StripItem(
            key=title, title=title, kind="movie", state="ready", tmdb_id=tmdb_id, ready_seconds=50
        )

    items = [ready("Ember Coast", 11), ready("Dry Harbour", 12)]
    told = _with_titles(conn, "k" * 64, items)

    assert [item.line for item in told] == [_WAITING, ""]


def test_the_strip_passes_on_no_other_service_s_poster(services, conn):
    show = strip.StripItem(
        key="k",
        title="Harbour",
        kind="show",
        state="downloading",
        poster_url="https://artworks.thetvdb.com/banners/posters/1.jpg",
    )

    (told,) = _with_titles(conn, "k" * 64, [show])

    assert told.poster_url == ""


def test_a_page_opened_while_a_service_is_down_claims_no_download_it_does_not_show(
    services, client, monkeypatch
):
    monkeypatch.setattr(fetcher, "_BLIP_SECONDS", 0.0)
    radarr, _, _, _ = services
    radarr.get_movies.return_value = [
        {**_movie(n, title), "hasFile": False, "isAvailable": True}
        for n, title in ((11, "Ember Coast"), (12, "Dry Harbour"))
    ]
    client.get("/api/downloads")
    radarr.get_queue.side_effect = radarr.get_movies.side_effect = requests.ConnectionError("x")
    client.get("/api/downloads")  # a blip that ends at once: the next missed answer is an outage

    data = client.get("/api/downloads").json()

    assert (data["hero"], data["queue"]) == (None, [])
    assert data["subtitle"] == "Can't reach Radarr"


def _show_downloading(tmdb_id: int, title: str) -> dict[str, object]:
    return {
        "title": f"{title}.S01E01.1080p",
        "series": {
            "id": tmdb_id,
            "title": title,
            "tmdbId": tmdb_id,
            "images": [{"coverType": "poster", "remoteUrl": "https://artworks.thetvdb.com/1.jpg"}],
        },
        "episode": {"seasonNumber": 1, "episodeNumber": 1, "title": "One"},
        "status": "downloading",
        "size": 100,
        "sizeleft": 40,
        "downloadId": str(tmdb_id),
    }


def test_a_show_whose_own_poster_is_another_service_s_gets_the_one_the_app_holds(
    services, client, conn, monkeypatch
):
    told = {77: {"poster_url": "/api/watch/img/tmdb/w342/s77.jpg"}}
    monkeypatch.setattr("mediaman.services.watch_catalog.held_details", lambda _k, n: told.get(n))
    insert_media_item(
        conn,
        id="501",
        title="Season 1",
        show_title="Grey Shore",
        media_type="tv_season",
        season_number=1,
        show_rating_key="500",
        tmdb_id=78,
    )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()
    services[1].get_queue.return_value = [
        _show_downloading(77, "Told Of"),
        _show_downloading(78, "Grey Shore"),
        _show_downloading(79, "Never Seen"),
    ]

    data = client.get("/api/downloads").json()
    on_strip = {
        i["title"]: i["poster_url"] for i in client.get("/api/downloads/strip").json()["items"]
    }

    posters = {i["title"]: i["poster_url"] for i in [data["hero"], *data["queue"]]}
    assert on_strip == posters
    assert posters == {
        "Told Of": "/api/watch/img/tmdb/w342/s77.jpg",
        "Grey Shore": "/api/poster/500",
        "Never Seen": "",
    }


@pytest.mark.parametrize(
    ("status", "tracked", "left", "expected"),
    [
        ("queued", "downloading", 1000, "queued"),
        ("paused", "downloading", 500, "queued"),
        ("delay", "", 1000, "queued"),
        ("warning", "downloading", 1000, "downloading"),
        ("downloadClientUnavailable", "downloading", 50, "searching"),
    ],
)
def test_a_film_nzbget_does_not_hold_reads_on_the_page_as_on_the_mailed_page(
    services, client, status, tracked, left, expected
):
    entry = {**_queued(status), "trackedDownloadState": tracked, "sizeleft": left}
    services[0].get_queue.return_value = [entry]

    hero = client.get("/api/downloads").json()["hero"]
    mailed = _radarr_queue_item([entry], 11)

    assert mailed is not None
    assert (hero["state"], mailed["state"]) == (expected, expected)


@pytest.mark.parametrize(("status", "expected"), [("delay", "queued"), ("paused", "queued")])
def test_a_show_nzbget_does_not_hold_reads_on_the_page_as_on_the_mailed_page(
    services, client, status, expected
):
    entry = {**_show_downloading(77, "Told Of"), "status": status}
    services[1].get_queue.return_value = [entry]

    hero = client.get("/api/downloads").json()["hero"]
    title, episodes = _sonarr_queue_entries([entry], 77)
    mailed = _sonarr_aggregate(title, episodes)

    assert (hero["state"], mailed["state"]) == (expected, expected)


def test_the_strip_says_ready_of_a_film_the_library_holds_as_the_page_does(services, client, conn):
    _hold_in_library(conn, 12, "Dry Harbour")
    now = now_utc().isoformat()
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at) VALUES"
        " ('radarr:Ember Coast#11', 'Ember Coast', 'movie', ?),"
        " ('radarr:Dry Harbour#12', 'Dry Harbour', 'movie', ?)",
        (now, now),
    )
    conn.commit()

    page = {r["title"]: r["readiness"] for r in client.get("/api/downloads").json()["recent"]}
    told = client.get("/api/downloads/strip").json()
    on_strip = {i["title"]: (i["playable"], i["line"]) for i in told["items"]}

    assert page == {"Ember Coast": "unknown", "Dry Harbour": "ready"}
    assert on_strip == {"Ember Coast": (False, _WAITING), "Dry Harbour": (True, "")}
    assert told["summary"]["ready"] == 1


@pytest.mark.parametrize(("held", "expected"), [(True, "ready"), (False, "almost_ready")])
def test_the_mailed_page_says_ready_only_once_the_library_holds_the_film(
    services, conn, monkeypatch, held, expected
):
    monkeypatch.setattr(
        "mediaman.web.routes.download.status.build_radarr_from_db", lambda *a: services[0]
    )
    if held:
        _hold_in_library(conn, 12, "Dry Harbour")
    services[0].get_movie_by_tmdb.return_value = {**_movie(12, "Dry Harbour"), "hasFile": True}

    assert _radarr_status(conn, "k" * 64, 12)["state"] == expected


def _film_in_queue(n: int, title: str, status: str) -> dict[str, object]:
    return {**_queued(status), "movie": _movie(n, title), "downloadId": str(n), "sizeleft": 500}


def test_the_top_card_stays_on_its_download_while_another_starts(services, client):
    radarr = services[0]
    radarr.get_queue.return_value = [
        _film_in_queue(1, "Zulu", "downloading"),
        _film_in_queue(2, "Alpha", "queued"),
    ]
    first = client.get("/api/downloads").json()["hero"]["title"]
    radarr.get_queue.return_value = [
        _film_in_queue(1, "Zulu", "downloading"),
        _film_in_queue(2, "Alpha", "downloading"),
    ]

    then = client.get("/api/downloads").json()

    assert (first, then["hero"]["title"]) == ("Zulu", "Zulu")
    assert [i["title"] for i in then["queue"]] == ["Alpha"]


def test_the_strip_holds_no_more_than_the_page_shows_while_a_service_is_down(
    services, client, monkeypatch
):
    monkeypatch.setattr(fetcher, "_BLIP_SECONDS", 0.0)
    radarr = services[0]
    radarr.get_movies.return_value = [
        {**_movie(n, title), "hasFile": False, "isAvailable": True}
        for n, title in ((11, "Ember Coast"), (12, "Dry Harbour"))
    ]
    client.get("/api/downloads")
    client.get("/api/downloads/strip")
    radarr.get_queue.side_effect = radarr.get_movies.side_effect = requests.ConnectionError("x")
    client.get("/api/downloads")  # a blip that ends at once: the next missed answer is an outage
    strip.shared.clear()

    data = client.get("/api/downloads").json()
    told = client.get("/api/downloads/strip").json()

    rows = (1 if data["hero"] else 0) + len(data["queue"])
    assert told["summary"]["total"] == data["strip"]["summary"]["total"] == rows


def test_a_viewer_is_not_told_which_service_does_not_answer(services, client, conn):
    _as_viewer(conn)
    services[0].get_queue.side_effect = requests.ConnectionError("down")
    services[2].get_queue.return_value = [
        {"NZBName": "Manual.2020", "Status": "DOWNLOADING", "FileSizeMB": 10, "RemainingSizeMB": 5}
    ]

    resp = client.get("/api/downloads")
    ring = client.get("/api/downloads/strip").json()

    assert resp.json()["unreachable"] == []
    assert resp.json()["outage"] == ring["outage"] == "Downloads aren't available"
    assert resp.json()["subtitle"] == "1 item in progress · downloads aren't available"
    assert "Radarr" not in resp.text


def _scanned(conn) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()


def _just_done(conn, dl_id: str, title: str, media_type: str = "movie") -> None:
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at) VALUES (?, ?, ?, ?)",
        (dl_id, title, media_type, now_utc().isoformat()),
    )
    conn.commit()


def _mailed_film(services, monkeypatch, tmdb_id: int, title: str, year: int = 2024) -> None:
    monkeypatch.setattr(
        "mediaman.web.routes.download.status.build_radarr_from_db", lambda *a: services[0]
    )
    services[0].get_movie_by_tmdb.return_value = {
        **_movie(tmdb_id, title),
        "year": year,
        "hasFile": True,
    }


def test_a_show_the_library_holds_but_sonarr_cannot_tell_of_is_not_offered_to_play(
    services, client, conn
):
    insert_media_item(
        conn,
        id="701",
        title="Season 1",
        show_title="Harrow Lane",
        media_type="tv_season",
        season_number=1,
        show_rating_key="700",
    )
    _scanned(conn)
    _just_done(conn, "sonarr:Harrow Lane", "Harrow Lane", "series")

    page = client.get("/api/downloads").json()["recent"]
    told = client.get("/api/downloads/strip").json()

    assert [r["readiness"] for r in page] == ["unknown"]
    assert [(i["playable"], i["line"]) for i in told["items"]] == [(False, _WAITING)]
    assert told["summary"]["ready"] == 0


@pytest.mark.parametrize(
    ("held_tmdb_id", "expected"),
    [(None, ("ready", True, "ready")), (99, ("unknown", False, "almost_ready"))],
)
def test_a_film_the_library_knows_by_title_alone_is_ready_but_another_of_its_title_is_not(
    services, client, conn, monkeypatch, held_tmdb_id, expected
):
    insert_media_item(conn, id="912", title="Dry Harbour", media_type="movie")
    conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = '912'", (held_tmdb_id,))
    _scanned(conn)
    _just_done(conn, "radarr:Dry Harbour#12", "Dry Harbour")
    _mailed_film(services, monkeypatch, 12, "Dry Harbour")

    (page,) = client.get("/api/downloads").json()["recent"]
    (item,) = client.get("/api/downloads/strip").json()["items"]

    said = (page["readiness"], item["playable"], _radarr_status(conn, "k" * 64, 12)["state"])
    assert said == expected


def test_the_mailed_page_does_not_take_a_film_of_its_title_from_another_year(
    services, conn, monkeypatch
):
    insert_media_item(
        conn,
        id="912",
        title="Dry Harbour",
        media_type="movie",
        file_path="/media/movies/Dry Harbour (1990)/Dry Harbour.mkv",
    )
    _scanned(conn)
    _mailed_film(services, monkeypatch, 12, "Dry Harbour", year=2024)

    assert _radarr_status(conn, "k" * 64, 12)["state"] == "almost_ready"


def test_a_done_film_the_strip_never_saw_is_not_taken_for_the_one_of_its_title_held(
    services, client, conn
):
    _hold_in_library(conn, 11, "Dune")
    _just_done(conn, "radarr:Dune#12", "Dune")

    (item,) = client.get("/api/downloads/strip").json()["items"]

    assert (item["playable"], item["line"], item["tmdb_ref"]) == (False, _WAITING, "tmdb-movie-12")


def test_stop_tracking_while_a_key_is_refused_does_not_say_to_try_again(services, client):
    services[0].get_queue.side_effect = SafeHTTPError(401, "", "http://radarr:7878/api/v3/queue")

    resp = client.post("/api/downloads/radarr%3ADune/abandon", json={})

    assert resp.status_code == 503
    assert resp.json()["message"] == "Can't sign in to Radarr. Check its details in Settings."
