"""The emailed download link for a title Radarr or Sonarr already holds."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.crypto import generate_download_token
from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.download import confirm as confirm_module
from mediaman.web.routes.download import reset_used_tokens
from mediaman.web.routes.download._tokens import _USED_TOKENS, _USED_TOKENS_LOCK
from mediaman.web.routes.download.submit import _DOWNLOAD_LIMITER_POST

_CONFIRM = "mediaman.web.routes.download.confirm"
_SUBMIT = "mediaman.web.routes.download.submit"


@pytest.fixture
def client(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    app = create_app()
    app.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    app.state.db = conn
    confirm_module._reset_arr_cache_for_tests()
    confirm_module._DOWNLOAD_LIMITER_GET.reset()
    _DOWNLOAD_LIMITER_POST.reset()
    reset_used_tokens()
    yield TestClient(app)
    conn.close()


def _token(secret_key, media_type, tmdb_id=500):
    return generate_download_token(
        email="viewer@example.com",
        action="download",
        title="Far Shore" if media_type == "tv" else "Dune",
        media_type=media_type,
        tmdb_id=tmdb_id,
        recommendation_id=None,
        secret_key=secret_key,
    )


def _consumed(token):
    with _USED_TOKENS_LOCK:
        return hashlib.sha256(token.encode()).hexdigest() in _USED_TOKENS


UNMONITORED_SHOW = {
    "tmdbId": 500,
    "monitored": False,
    "seasons": [{"seasonNumber": 1, "monitored": False}],
}


class TestConfirmPage:
    def _page(self, client, secret_key, media_type, *, series=None, movie=None):
        sonarr_cache = {"sonarr_series": {500: series} if series else {}}
        radarr_cache = {"radarr_movies": {500: movie} if movie else {}}
        with (
            patch(f"{_CONFIRM}._get_sonarr_cache_cached", return_value=sonarr_cache),
            patch(f"{_CONFIRM}._get_radarr_cache_cached", return_value=radarr_cache),
        ):
            return client.get(f"/download/{_token(secret_key, media_type)}")

    def test_a_show_sonarr_holds_unmonitored_says_so_and_offers_no_download(
        self, client, secret_key
    ):
        resp = self._page(client, secret_key, "tv", series=UNMONITORED_SHOW)

        assert resp.status_code == 200
        assert "Already tracked by Sonarr" in resp.text
        assert 'id="btn-download"' not in resp.text

    def test_a_show_sonarr_does_not_hold_still_offers_the_download(self, client, secret_key):
        resp = self._page(client, secret_key, "tv")

        assert 'id="btn-download"' in resp.text
        assert "Already tracked" not in resp.text

    def test_an_unmonitored_movie_keeps_its_download_button(self, client, secret_key):
        movie = {"tmdbId": 500, "hasFile": False, "monitored": False}

        resp = self._page(client, secret_key, "movie", movie=movie)

        assert 'id="btn-download"' in resp.text
        assert "Already tracked" not in resp.text


class TestSubmitRefusedForAHeldTitle:
    def _post(self, client, secret_key, media_type, arr, builder):
        token = _token(secret_key, media_type)
        with patch(f"{_SUBMIT}.{builder}", return_value=arr):
            return token, client.post(f"/download/{token}")

    def test_an_abandoned_movie_is_re_monitored_and_the_link_is_used(self, client, secret_key):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False}

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 200
        radarr.remonitor_movie.assert_called_once_with(7)
        radarr.add_movie.assert_not_called()
        assert _consumed(token)

    def test_an_unmonitored_movie_that_has_its_file_is_left_alone_and_the_link_is_used(
        self, client, secret_key
    ):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False, "hasFile": True}

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
        radarr.remonitor_movie.assert_not_called()
        radarr.add_movie.assert_not_called()
        assert _consumed(token)

    def test_a_movie_radarr_holds_monitored_is_already_exists_and_the_link_is_used(
        self, client, secret_key
    ):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": True}

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
        assert "poll_token" in resp.json()
        radarr.add_movie.assert_not_called()
        assert _consumed(token)

    def test_a_movie_add_that_raced_a_400_is_already_exists_and_the_link_is_used(
        self, client, secret_key
    ):
        radarr = MagicMock()
        held = {"id": 7, "monitored": True}
        radarr.get_movie_by_tmdb.side_effect = [None, held, held]
        radarr.add_movie.side_effect = SafeHTTPError(400, "already been added", "radarr/m")

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 409
        assert _consumed(token)

    def test_a_failed_re_monitor_is_a_502_and_frees_the_link(self, client, secret_key):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False}
        radarr.remonitor_movie.side_effect = SafeHTTPError(409, "refused", "radarr/m")

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 502
        assert not _consumed(token)
        radarr.unmonitor_movie.assert_called_once_with(7, max_retries=1)

    def test_a_400_for_a_series_sonarr_tracks_is_already_exists_and_the_token_stays_used(
        self, client, secret_key
    ):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        sonarr.add_series.side_effect = SafeHTTPError(400, "already been added", "sonarr/s")
        sonarr.get_series.return_value = [{"tvdbId": 999}]

        token, resp = self._post(client, secret_key, "tv", sonarr, "build_sonarr_from_db")

        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
        assert _consumed(token)

    def test_a_400_for_a_series_sonarr_holds_but_will_not_fetch_is_already_exists_and_frees_the_link(
        self, client, secret_key
    ):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        sonarr.add_series.side_effect = SafeHTTPError(400, "already been added", "sonarr/s")
        sonarr.get_series.return_value = [{"tvdbId": 999, "monitored": False, "seasons": []}]

        token, resp = self._post(client, secret_key, "tv", sonarr, "build_sonarr_from_db")

        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
        assert not _consumed(token)

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_a_400_for_a_title_the_arr_does_not_hold_stays_a_502_and_frees_the_token(
        self, client, secret_key, media_type
    ):
        arr = MagicMock()
        arr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        arr.get_movie_by_tmdb.return_value = None
        arr.get_series.return_value = []
        arr.add_movie.side_effect = SafeHTTPError(400, "Invalid root folder", "arr/x")
        arr.add_series.side_effect = SafeHTTPError(400, "Invalid root folder", "arr/x")
        builder = "build_radarr_from_db" if media_type == "movie" else "build_sonarr_from_db"

        token, resp = self._post(client, secret_key, media_type, arr, builder)

        assert resp.status_code == 502
        assert not _consumed(token)
