"""A re-download failure is only reported as a duplicate when the add call was refused."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import RADARR_SPEC, SONARR_SPEC
from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import router as library_api_router
from mediaman.web.routes.library_api.redownload import _REDOWNLOAD_LIMITER

RADARR = "mediaman.web.routes.library_api.build_radarr_from_db"
SONARR = "mediaman.web.routes.library_api.build_sonarr_from_db"


@pytest.fixture(autouse=True)
def _reset_limiter():
    _REDOWNLOAD_LIMITER.reset()


def _err(status: int, body: str = "conflict") -> SafeHTTPError:
    return SafeHTTPError(status_code=status, body_snippet=body, url="http://arr/api/v3/x")


def _client(app_factory, authed_client, conn):
    app = app_factory(library_router, library_api_router, conn=conn)
    return authed_client(app, conn)


@pytest.mark.parametrize("status", [409, 422])
def test_failed_movie_remonitor_is_a_failure_not_a_duplicate(
    app_factory, authed_client, conn, status
):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"id": 7}
    radarr.remonitor_movie.side_effect = _err(status)
    client = _client(app_factory, authed_client, conn)
    with patch(RADARR, return_value=radarr):
        resp = client.post("/api/media/redownload", json={"title": "Dune", "tmdb_id": 42})
    assert resp.json()["ok"] is False
    assert "already exists" not in resp.json()["error"]


def test_failed_season_remonitor_is_a_failure_not_a_duplicate(app_factory, authed_client, conn):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = {"id": 3}
    sonarr.remonitor_season.side_effect = _err(409)
    client = _client(app_factory, authed_client, conn)
    with patch(SONARR, return_value=sonarr):
        resp = client.post(
            "/api/media/redownload",
            json={"title": "Severance", "tvdb_id": 999, "season_number": 1},
        )
    assert resp.json()["ok"] is False
    assert "already exists" not in resp.json()["error"]


def _post(client, payload):
    return client.post("/api/media/redownload", json=payload)


def test_every_failure_carries_its_human_reason_under_message(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)

    with patch(RADARR, return_value=None):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert (resp.status_code, resp.json()["message"]) == (409, "Radarr is not configured")
    assert resp.json()["error"] == "Radarr is not configured"

    resp = _post(client, {"title": "  "})
    assert (resp.status_code, resp.json()["message"]) == (400, "No title provided")

    resp = _post(client, {"title": "Dune"})
    assert resp.status_code == 400
    assert resp.json()["message"] == resp.json()["error"]
    assert "tmdb_id" in resp.json()["message"]

    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = _err(409)
    with patch(RADARR, return_value=radarr):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["message"] == "'Dune' already exists in Radarr"

    radarr.add_movie.side_effect = _err(500, "boom")
    with patch(RADARR, return_value=radarr):
        resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["message"].startswith("Download request failed")
    assert resp.json()["error"] == resp.json()["message"]


def test_throttled_redownload_carries_its_reason_under_message(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)
    with patch(RADARR, return_value=None):
        for _ in range(21):
            resp = _post(client, {"title": "Dune", "tmdb_id": 42})
    assert resp.status_code == 429
    assert resp.json()["message"] == "Too many redownload requests — slow down"


class _OneTitleArr(ArrClient):
    """The real client over one in-memory Arr entry; every search command fails.

    *commands* is the Arr's own list of them, or the error reading it raises.
    """

    def __init__(self, spec, entry, error, commands=()):
        super().__init__(spec, "http://arr.local", "k")
        self.entry = entry
        self._error = error
        self._commands = commands

    def _get(self, path):
        if path == "/api/v3/command":
            if isinstance(self._commands, Exception):
                raise self._commands
            return list(self._commands)
        entry = copy.deepcopy(self.entry)
        return [entry] if "?" in path else entry

    def _put(self, path, data):
        self.entry = copy.deepcopy(data)

    def _post(self, path, data):
        raise self._error


_SEARCH_ERRORS = pytest.mark.parametrize(
    "error", [_err(503), requests.ReadTimeout("slow")], ids=["refused", "timeout"]
)


def _movie(monitored):
    return {"id": 7, "tmdbId": 42, "monitored": monitored, "hasFile": False}


def _series(monitored):
    seasons = [
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": monitored},
    ]
    return {"id": 3, "tvdbId": 999, "monitored": True, "seasons": seasons}


@_SEARCH_ERRORS
def test_a_movie_whose_search_fails_is_left_unmonitored_as_it_was(
    app_factory, authed_client, conn, error
):
    radarr = _OneTitleArr(RADARR_SPEC, _movie(False), error)
    with patch(RADARR, return_value=radarr):
        resp = _post(_client(app_factory, authed_client, conn), {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["ok"] is False
    assert radarr.entry["monitored"] is False


@_SEARCH_ERRORS
def test_a_season_whose_search_fails_is_left_unmonitored_as_it_was(
    app_factory, authed_client, conn, error
):
    sonarr = _OneTitleArr(SONARR_SPEC, _series(False), error)
    with patch(SONARR, return_value=sonarr):
        resp = _post(
            _client(app_factory, authed_client, conn),
            {"title": "Severance", "tvdb_id": 999, "season_number": 2},
        )
    assert resp.json()["ok"] is False
    assert [s["monitored"] for s in sonarr.entry["seasons"]] == [True, False]


_MOVIE_SEARCH = {"name": "MoviesSearch", "status": "started", "body": {"movieIds": [7]}}
_SEASON_SEARCH = {
    "name": "SeasonSearch",
    "status": "queued",
    "body": {"seriesId": 3, "seasonNumber": 2},
}


@_SEARCH_ERRORS
def test_a_search_that_got_no_answer_but_was_started_is_a_success(
    app_factory, authed_client, conn, error
):
    client = _client(app_factory, authed_client, conn)
    radarr = _OneTitleArr(RADARR_SPEC, _movie(False), error, [_MOVIE_SEARCH])
    sonarr = _OneTitleArr(SONARR_SPEC, _series(False), error, [_SEASON_SEARCH])
    with patch(RADARR, return_value=radarr), patch(SONARR, return_value=sonarr):
        movie = _post(client, {"title": "Dune", "tmdb_id": 42})
        season = _post(client, {"title": "Severance", "tvdb_id": 999, "season_number": 2})
    assert (movie.json()["ok"], season.json()["ok"]) == (True, True)
    assert radarr.entry["monitored"] is True
    assert [s["monitored"] for s in sonarr.entry["seasons"]] == [True, True]


@_SEARCH_ERRORS
def test_a_search_nobody_can_tell_the_state_of_is_a_failure_that_unmonitors_nothing(
    app_factory, authed_client, conn, error
):
    client = _client(app_factory, authed_client, conn)
    radarr = _OneTitleArr(RADARR_SPEC, _movie(False), error, error)
    sonarr = _OneTitleArr(SONARR_SPEC, _series(False), error, error)
    with patch(RADARR, return_value=radarr), patch(SONARR, return_value=sonarr):
        movie = _post(client, {"title": "Dune", "tmdb_id": 42})
        season = _post(client, {"title": "Severance", "tvdb_id": 999, "season_number": 2})
    for resp in (movie, season):
        assert (resp.status_code, resp.json()["ok"]) == (200, False)
        assert resp.json()["message"]
    assert radarr.entry["monitored"] is True
    assert [s["monitored"] for s in sonarr.entry["seasons"]] == [True, True]


def test_a_season_that_was_never_monitored_keeps_its_lone_monitored_episode(
    app_factory, authed_client, conn
):
    class _RefusesWrites(_OneTitleArr):
        writes: list[str] = []

        def _get(self, path):
            if path.startswith("/api/v3/episode?"):
                return [{"id": 21, "seasonNumber": 2, "monitored": True}]
            return super()._get(path)

        def _put(self, path, data):
            if path == "/api/v3/episode/monitor":
                self.writes.append(path)
                return
            raise _err(400)

    sonarr = _RefusesWrites(SONARR_SPEC, _series(False), _err(400))
    with patch(SONARR, return_value=sonarr):
        resp = _post(
            _client(app_factory, authed_client, conn),
            {"title": "Severance", "tvdb_id": 999, "season_number": 2},
        )
    assert resp.json()["ok"] is False
    assert sonarr.writes == []


def test_a_title_monitored_before_a_failed_search_stays_monitored(app_factory, authed_client, conn):
    client = _client(app_factory, authed_client, conn)
    radarr = _OneTitleArr(RADARR_SPEC, _movie(True), _err(503))
    sonarr = _OneTitleArr(SONARR_SPEC, _series(True), _err(503))
    with patch(RADARR, return_value=radarr), patch(SONARR, return_value=sonarr):
        _post(client, {"title": "Dune", "tmdb_id": 42})
        _post(client, {"title": "Severance", "tvdb_id": 999, "season_number": 2})
    assert radarr.entry["monitored"] is True
    assert [s["monitored"] for s in sonarr.entry["seasons"]] == [True, True]


def test_a_failed_re_download_still_drops_the_emailed_pages_arr_lists(
    app_factory, authed_client, conn, monkeypatch
):
    from mediaman.web.routes.download import confirm

    monkeypatch.setitem(confirm._RADARR_CACHE, "held", (0.0, MagicMock()))
    monkeypatch.setitem(confirm._SONARR_CACHE, "held", (0.0, MagicMock()))
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"id": 7}
    radarr.remonitor_movie.side_effect = _err(500)
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = {"id": 3}
    sonarr.remonitor_season.side_effect = _err(500)
    client = _client(app_factory, authed_client, conn)

    with patch(RADARR, return_value=radarr):
        _post(client, {"title": "Dune", "tmdb_id": 42})
    assert confirm._RADARR_CACHE == {} and confirm._SONARR_CACHE == {}

    monkeypatch.setitem(confirm._RADARR_CACHE, "held", (0.0, MagicMock()))
    monkeypatch.setitem(confirm._SONARR_CACHE, "held", (0.0, MagicMock()))
    with patch(SONARR, return_value=sonarr):
        _post(client, {"title": "Severance", "tvdb_id": 999, "season_number": 1})
    assert confirm._RADARR_CACHE == {} and confirm._SONARR_CACHE == {}


@pytest.mark.parametrize("status", [0, 504])
def test_a_movie_add_that_timed_out_but_landed_is_a_success(
    app_factory, authed_client, conn, status
):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.side_effect = [None, {"id": 7, "monitored": True}]
    radarr.add_movie.side_effect = _err(status)
    with patch(RADARR, return_value=radarr):
        resp = _post(_client(app_factory, authed_client, conn), {"title": "Dune", "tmdb_id": 42})
    assert resp.json() == {"ok": True, "message": "Added 'Dune' to Radarr"}
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 're_downloaded'").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("status", [0, 504])
def test_a_season_add_that_timed_out_but_landed_is_a_success(
    app_factory, authed_client, conn, status
):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = None
    sonarr.get_series.return_value = [{"tvdbId": 999, "monitored": True}]
    sonarr.add_series_with_seasons.side_effect = _err(status)
    with patch(SONARR, return_value=sonarr):
        resp = _post(
            _client(app_factory, authed_client, conn),
            {"title": "Severance", "tvdb_id": 999, "season_number": 2},
        )
    assert resp.json() == {"ok": True, "message": "Added season 2 of 'Severance' to Sonarr"}


def test_a_held_series_with_no_id_is_a_failure_and_is_not_added_again(
    app_factory, authed_client, conn
):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = {"seasons": []}
    sonarr.get_series.return_value = [{"tvdbId": 999, "monitored": True}]
    sonarr.add_series_with_seasons.side_effect = _err(0, "timeout")
    with patch(SONARR, return_value=sonarr):
        resp = _post(
            _client(app_factory, authed_client, conn),
            {"title": "Severance", "tvdb_id": 999, "season_number": 2},
        )
    assert resp.json()["ok"] is False
    sonarr.add_series_with_seasons.assert_not_called()


def test_a_held_movie_with_no_id_is_a_failure_and_is_not_added_again(
    app_factory, authed_client, conn
):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"monitored": True}
    radarr.add_movie.side_effect = _err(0, "timeout")
    with patch(RADARR, return_value=radarr):
        resp = _post(_client(app_factory, authed_client, conn), {"title": "Dune", "tmdb_id": 42})
    assert resp.json()["ok"] is False
    radarr.add_movie.assert_not_called()


@pytest.mark.parametrize(
    ("files", "total", "fetched"), [(8, 8, False), (3, 8, True)], ids=["complete", "part"]
)
def test_a_season_complete_on_disk_is_refused_and_one_with_gaps_is_fetched(
    app_factory, authed_client, conn, files, total, fetched
):
    # An unmonitored episode with no file is in the total alone.
    stats = {"episodeFileCount": files, "episodeCount": files, "totalEpisodeCount": total}
    series = _series(False)
    series["seasons"][1]["statistics"] = stats
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = series
    with patch(SONARR, return_value=sonarr):
        resp = _post(
            _client(app_factory, authed_client, conn),
            {"title": "Severance", "tvdb_id": 999, "season_number": 2},
        )

    assert resp.status_code == (200 if fetched else 409)
    assert resp.json()["ok"] is fetched
    assert sonarr.remonitor_season.called is fetched
    audited = conn.execute("SELECT 1 FROM audit_log WHERE action = 're_downloaded'").fetchall()
    assert bool(audited) is fetched


def test_season_0_is_answered_and_audited_as_specials(app_factory, authed_client, conn):
    specials = {"seasonNumber": 0, "monitored": True}
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.side_effect = [
        None,
        {"id": 3, "seasons": [specials]},
        {
            "id": 3,
            "seasons": [{**specials, "statistics": {"episodeCount": 1, "episodeFileCount": 1}}],
        },
    ]
    client = _client(app_factory, authed_client, conn)
    asked = {"title": "Severance", "tvdb_id": 999, "season_number": 0}
    with patch(SONARR, return_value=sonarr):
        added, held, on_disk = (_post(client, asked).json() for _ in range(3))

    assert added["message"] == "Added specials of 'Severance' to Sonarr"
    assert held["message"].startswith("Re-monitored specials of 'Severance'")
    assert on_disk["error"] == "Specials of 'Severance' already exist in Sonarr"
    audits = conn.execute("SELECT detail FROM audit_log WHERE action = 're_downloaded'").fetchall()
    assert {a["detail"] for a in audits} == {"Re-downloaded specials of 'Severance' by admin"}
