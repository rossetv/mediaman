"""Downloads, the strip and the mailed page call a finished download ready only once the library
holds that very title: not a namesake, and of a show not before the library has read it since."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from mediaman.core.time import now_utc
from mediaman.services.downloads import strip
from mediaman.services.downloads.download_queue import _reset_previous_queue
from mediaman.services.watch_catalog import ArrView, LibraryIndex, clear_library_index, invalidate
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.download.status import _radarr_status
from mediaman.web.routes.downloads import router as downloads_router
from mediaman.web.routes.downloads_strip import _with_title, library_title
from mediaman.web.routes.downloads_strip import router as strip_router
from tests.helpers.factories import insert_media_item

_WAITING = "In your library soon"


def _clear() -> None:
    _reset_previous_queue()
    strip.shared.clear()
    strip.on_page.clear()
    clear_library_index()
    invalidate()


@pytest.fixture
def services(monkeypatch):
    """Radarr, Sonarr and NZBGet, all set up and idle."""
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
            f"mediaman.services.arr.build.build_{name}_from_db", lambda *a, name=name: built[name]
        )
    monkeypatch.setattr(
        "mediaman.services.downloads.download_queue.build_nzbget_from_db", lambda *a: nzbget
    )
    monkeypatch.setattr(
        "mediaman.web.routes.download.status.build_radarr_from_db", lambda *a: radarr
    )
    yield radarr, sonarr
    _clear()


@pytest.fixture
def client(app_factory, conn, templates_stub):
    from fastapi.testclient import TestClient

    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, created_at)"
        " VALUES ('admin', 'x', 'admin', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('plex_libraries', '[1]', '2026-01-01')"
    )
    conn.commit()
    app = app_factory(
        downloads_router, strip_router, conn=conn, state_extras={"templates": templates_stub}
    )
    client = TestClient(app)
    client.cookies.set("session_token", create_session(conn, "admin"))
    return client


def _done(conn, dl_id: str, title: str, media_type: str, at) -> None:
    conn.execute(
        "INSERT INTO recent_downloads (dl_id, title, media_type, completed_at) VALUES (?, ?, ?, ?)",
        (dl_id, title, media_type, at.isoformat()),
    )
    conn.commit()


def _said(client) -> tuple[list[str], list[tuple[bool, str]]]:
    page = [r["readiness"] for r in client.get("/api/downloads").json()["recent"]]
    told = client.get("/api/downloads/strip").json()["items"]
    return page, [(i["playable"], i["line"]) for i in told]


@pytest.mark.parametrize(
    ("read_after", "expected"),
    [(False, (["unknown"], [(False, _WAITING)])), (True, (["ready"], [(True, "")]))],
)
def test_a_new_episode_of_a_held_show_is_ready_only_once_the_library_has_read_the_show_since(
    services, client, conn, read_after, expected
):
    done = now_utc() - timedelta(seconds=10)
    read = done + timedelta(seconds=5 if read_after else -3600)
    insert_media_item(
        conn,
        id="701",
        title="Season 2",
        show_title="Harrow Lane",
        media_type="tv_season",
        season_number=2,
        show_rating_key="700",
        last_scanned_at=read,
    )
    services[1].get_series.return_value = [
        {
            "id": 7,
            "title": "Harrow Lane",
            "seasons": [
                {
                    "seasonNumber": 2,
                    "monitored": True,
                    "statistics": {
                        "episodeFileCount": 8,
                        "episodeCount": 8,
                        "previousAiring": "2020-01-01T00:00:00Z",
                    },
                }
            ],
            "statistics": {"episodeFileCount": 8},
        }
    ]
    _done(conn, "sonarr:Harrow Lane", "Harrow Lane", "series", done)

    assert _said(client) == expected


def test_a_film_held_by_title_from_another_year_is_not_the_one_downloaded(
    services, client, conn, monkeypatch
):
    insert_media_item(
        conn,
        id="912",
        title="Dry Harbour",
        media_type="movie",
        file_path="/media/movies/Dry Harbour (1990)/Dry Harbour.mkv",
    )
    told = {("movie", 12): {"year": 2024}}
    monkeypatch.setattr(
        "mediaman.services.watch_catalog.held_details", lambda kind, n: told.get((kind, n))
    )
    _done(conn, "radarr:Dry Harbour#12", "Dry Harbour", "movie", now_utc())

    assert _said(client) == (["unknown"], [(False, _WAITING)])


def test_the_mailed_page_says_a_downloaded_film_waits_for_the_library(services, conn):
    services[0].get_movie_by_tmdb.return_value = {
        "id": 12,
        "tmdbId": 12,
        "title": "Dry Harbour",
        "year": 2024,
        "hasFile": True,
    }

    said = _radarr_status(conn, "k" * 64, 12)

    assert (said["state"], said["state_label"]) == ("almost_ready", _WAITING)


def _index_with_old_show() -> LibraryIndex:
    """A library holding the 1980 Harrow Lane, known to TMDB as 70."""
    return LibraryIndex.build([], [("700", "Harrow Lane", None)], ids={"show-700": (70, 1980)})


def test_a_remake_of_a_held_show_is_not_taken_for_it():
    index = _index_with_old_show()

    assert library_title(index, "tv", 71, "Harrow Lane") is None
    assert library_title(index, "tv", 70, "Harrow Lane") == "show-700"


def test_a_show_whose_tmdb_id_the_library_does_not_know_is_still_found_by_title():
    index = LibraryIndex.build([], [("700", "Harrow Lane", None)])

    assert library_title(index, "tv", 71, "Harrow Lane") == "show-700"


def test_the_strip_does_not_open_the_held_show_for_its_remake(monkeypatch):
    monkeypatch.setattr(
        "mediaman.web.routes.downloads_strip.title_art",
        lambda key: {"poster_url": "", "backdrop_url": ""},
    )
    remake = {"tmdbId": 71, "seasons": [{"seasonNumber": 1, "statistics": {}}]}
    arr = ArrView(
        {
            "radarr_movies": {},
            "radarr_queue_tmdb_ids": set(),
            "sonarr_series": {71: remake},
            "sonarr_queue_tmdb_ids": set(),
        }
    )
    monkeypatch.setattr(ArrView, "state", lambda self, kind, n: ("in_library", None))
    item = strip.StripItem(key="k", title="Harrow Lane", kind="show", state="ready", tmdb_id=71)

    shown = _with_title(item, _index_with_old_show(), arr, shown=False)

    assert (shown.ref, shown.href) == ("tmdb-tv-71", "/#title=tmdb-tv-71")
