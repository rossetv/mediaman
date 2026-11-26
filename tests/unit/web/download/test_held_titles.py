"""The emailed download link for a title Radarr or Sonarr already holds."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mediaman.core.deleted_media import record_deleted_media
from mediaman.crypto import generate_download_token
from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.scanner.repository.scheduled_actions import DeletionRow, was_requested_since
from mediaman.services.arr import ArrError
from mediaman.services.arr.base import SearchUnanswered
from mediaman.services.arr.state import compute_download_state
from mediaman.services.infra import SafeHTTPError
from mediaman.web.routes.download import confirm as confirm_module
from mediaman.web.routes.download import reset_used_tokens
from mediaman.web.routes.download._tokens import _USED_TOKENS, _USED_TOKENS_LOCK
from mediaman.web.routes.download.status import _sonarr_series_fallback
from mediaman.web.routes.download.submit import _DOWNLOAD_LIMITER_POST
from tests.helpers.factories import insert_audit_log

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


def _token(secret_key, media_type, tmdb_id=500, email="viewer@example.com"):
    return generate_download_token(
        email=email,
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
        assert "Already on the list" in resp.text
        assert 'id="btn-download"' not in resp.text

    def test_a_show_sonarr_does_not_hold_still_offers_the_download(self, client, secret_key):
        resp = self._page(client, secret_key, "tv")

        assert 'id="btn-download"' in resp.text
        assert '"download_state": "tracked"' not in resp.text

    def test_an_unmonitored_movie_keeps_its_download_button(self, client, secret_key):
        movie = {"tmdbId": 500, "hasFile": False, "monitored": False}

        resp = self._page(client, secret_key, "movie", movie=movie)

        assert 'id="btn-download"' in resp.text
        assert '"download_state": "tracked"' not in resp.text

    @pytest.mark.parametrize("season_files", [0, 4])
    def test_a_show_still_downloading_is_on_its_way_not_offered_again(
        self, client, secret_key, season_files
    ):
        aired = {"episodeFileCount": season_files, "episodeCount": 8, "previousAiring": "2026"}
        series = {
            "tmdbId": 500,
            "monitored": True,
            "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": aired}],
        }
        cache = {"sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": {500}}
        with patch(f"{_CONFIRM}._get_sonarr_cache_cached", return_value=cache):
            resp = client.get(f"/download/{_token(secret_key, 'tv')}")

        assert '"download_state": "queued"' in resp.text
        assert 'id="btn-download"' not in resp.text

    def test_a_movie_in_the_queue_is_on_its_way_not_offered_again(self, client, secret_key):
        cache = {
            "radarr_movies": {500: {"tmdbId": 500, "monitored": True}},
            "radarr_queue_tmdb_ids": {500},
        }
        with patch(f"{_CONFIRM}._get_radarr_cache_cached", return_value=cache):
            resp = client.get(f"/download/{_token(secret_key, 'movie')}")

        assert '"download_state": "queued"' in resp.text

    @pytest.mark.parametrize(("monitored", "state"), [(True, "queued"), (False, "tracked")])
    def test_a_show_part_there_is_on_its_way_only_when_sonarr_monitors_it(
        self, client, secret_key, monitored, state
    ):
        aired = {"episodeFileCount": 4, "episodeCount": 8, "previousAiring": "2026"}
        series = {
            "tmdbId": 500,
            "monitored": monitored,
            "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": aired}],
        }

        resp = self._page(client, secret_key, "tv", series=series)

        assert f'"download_state": "{state}"' in resp.text

    @pytest.mark.parametrize(
        ("seasons", "files", "state"),
        [
            pytest.param(
                [
                    (False, {"episodeFileCount": 8, "episodeCount": 8, "previousAiring": "2026"}),
                    (True, {"totalEpisodeCount": 8, "nextAiring": "2027"}),
                ],
                8,
                "in_library",
                id="all-aired-there",
            ),
            pytest.param(
                [(True, {"totalEpisodeCount": 8, "nextAiring": "2027"})],
                0,
                "tracked",
                id="nothing-aired",
            ),
            pytest.param(
                [
                    (True, {"totalEpisodeCount": 8, "nextAiring": "2027"}),
                    (False, {"episodeFileCount": 2, "episodeCount": 3, "previousAiring": "2026"}),
                ],
                2,
                "queued",
                id="one-aired-episode-missing",
            ),
            pytest.param([(True, {})], 0, "queued", id="just-added"),
        ],
    )
    def test_a_monitored_show_is_on_its_way_only_when_an_aired_episode_is_missing(
        self, client, secret_key, seasons, files, state
    ):
        series = {
            "tmdbId": 500,
            "monitored": True,
            "statistics": {"episodeFileCount": files},
            "seasons": [
                {"seasonNumber": n, "monitored": monitored, "statistics": stats}
                for n, (monitored, stats) in enumerate(seasons, 1)
            ],
        }

        resp = self._page(client, secret_key, "tv", series=series)

        assert f'"download_state": "{state}"' in resp.text


def _season_of(number, files, wanted, *, monitored=False, aired=True):
    stats = {"episodeFileCount": files, "episodeCount": wanted, "totalEpisodeCount": 10}
    if aired:
        stats["previousAiring"] = "2020-01-01T00:00:00Z"
    return {"seasonNumber": number, "monitored": monitored, "statistics": stats}


@pytest.mark.parametrize(
    ("seasons", "state"),
    [
        ([_season_of(1, 10, 10), _season_of(2, 10, 10)], "in_library"),
        ([_season_of(1, 10, 10), _season_of(0, 0, 1)], "in_library"),
        ([_season_of(1, 10, 10, monitored=True), _season_of(2, 3, 3)], "in_library"),
        ([_season_of(1, 10, 10, monitored=True), _season_of(2, 1, 2)], "queued"),
        ([_season_of(1, 0, 1)], "queued"),
        ([_season_of(1, 1, 1)], "tracked"),
        ([_season_of(1, 3, 3)], "tracked"),
        ([_season_of(1, 1, 1, monitored=True, aired=False)], "tracked"),
        ([_season_of(1, 10, 10, monitored=True, aired=False)], "in_library"),
    ],
    ids=[
        "whole but not monitored",
        "whole but for a wanted special",
        "whole beside one abandoned in part",
        "whole beside one lacking a wanted episode",
        "one episode wanted",
        "that one episode there",
        "abandoned in part",
        "one file before it airs",
        "whole and undated",
    ],
)
def test_the_emailed_page_the_status_and_the_state_say_the_same_of_a_show(
    client, secret_key, seasons, state
):
    files = sum(s["statistics"]["episodeFileCount"] for s in seasons)
    series = {
        "tmdbId": 500,
        "title": "Far Shore",
        "monitored": True,
        "statistics": {"episodeFileCount": files},
        "seasons": seasons,
    }
    cache = {"sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": set()}
    with patch(f"{_CONFIRM}._get_sonarr_cache_cached", return_value=cache):
        page = client.get(f"/download/{_token(secret_key, 'tv')}").text
    sonarr = MagicMock()
    sonarr.get_series.return_value = [series]

    assert f'"download_state": "{state}"' in page
    ready = state == "in_library"
    assert (_sonarr_series_fallback(sonarr, 500)["state"] == "ready") is ready
    caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(), **cache}
    assert (compute_download_state("tv", 500, caches) == "in_library") is ready


@pytest.mark.parametrize(
    ("seasons", "in_queue", "card", "page"),
    [
        (
            [_season_of(1, 3, 3), _season_of(2, 0, 10, monitored=True)],
            True,
            "downloading",
            "queued",
        ),
        ([_season_of(1, 3, 3), _season_of(2, 0, 10, monitored=True)], False, "queued", "queued"),
        (
            [_season_of(1, 3, 3), _season_of(2, 0, 0, monitored=True, aired=False)],
            True,
            "downloading",
            "queued",
        ),
        (
            [_season_of(1, 3, 3), _season_of(2, 0, 0, monitored=True, aired=False)],
            False,
            "queued",
            "tracked",
        ),
        (
            [_season_of(1, 10, 10), _season_of(2, 0, 10, monitored=True)],
            True,
            "downloading",
            "queued",
        ),
        ([_season_of(1, 3, 3)], False, None, "tracked"),
    ],
    ids=[
        "another season downloading",
        "another season searched",
        "an early episode downloading",
        "another season still to air",
        "one whole and another downloading",
        "nothing else",
    ],
)
def test_what_is_left_of_a_season_nobody_wants_hides_no_season_on_its_way(
    client, secret_key, seasons, in_queue, card, page
):
    files = sum(s["statistics"]["episodeFileCount"] for s in seasons)
    series = {
        "tmdbId": 500,
        "monitored": True,
        "statistics": {"episodeFileCount": files},
        "seasons": seasons,
    }
    cache = {"sonarr_series": {500: series}, "sonarr_queue_tmdb_ids": {500} if in_queue else set()}
    with patch(f"{_CONFIRM}._get_sonarr_cache_cached", return_value=cache):
        shown = client.get(f"/download/{_token(secret_key, 'tv')}").text

    caches = {"radarr_movies": {}, "radarr_queue_tmdb_ids": set(), **cache}
    assert compute_download_state("tv", 500, caches) == card
    assert f'"download_state": "{page}"' in shown


class TestReopeningAUsedLink:
    """The page keeps the Arr list for half a minute; a request must not be hidden by it."""

    def _radarr(self):
        movies = []
        radarr = MagicMock()
        radarr.get_movies.side_effect = lambda: list(movies)
        radarr.get_movie_by_tmdb.return_value = None
        radarr.get_queue.return_value = []
        radarr.add_movie.side_effect = lambda *a: movies.append(
            {"tmdbId": 500, "title": "Dune", "monitored": True}
        )
        return radarr

    def test_a_used_link_reads_queued_at_once(self, client, secret_key):
        radarr, token = self._radarr(), _token(secret_key, "movie")
        with (
            patch(f"{_CONFIRM}.build_radarr_from_db", return_value=radarr),
            patch(f"{_SUBMIT}.build_radarr_from_db", return_value=radarr),
        ):
            assert 'id="btn-download"' in client.get(f"/download/{token}").text
            assert client.post(f"/download/{token}").status_code == 200
            reopened = client.get(f"/download/{token}").text

        assert 'id="btn-download"' not in reopened
        assert '"download_state": "queued"' in reopened

    def test_a_list_fetched_before_the_request_is_not_kept_after_it(self, client, secret_key):
        radarr, token = self._radarr(), _token(secret_key, "movie")
        radarr.get_queue.side_effect = lambda: confirm_module.drop_arr_cache() or []
        with patch(f"{_CONFIRM}.build_radarr_from_db", return_value=radarr):
            client.get(f"/download/{token}")
            radarr.add_movie()
            reopened = client.get(f"/download/{token}").text

        assert '"download_state": "queued"' in reopened


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

    def test_a_re_monitored_movie_whose_search_got_no_answer_is_on_its_way(
        self, client, secret_key
    ):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False}
        radarr.remonitor_movie.side_effect = SearchUnanswered("no answer")

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 200
        assert _consumed(token)
        mails = client.app.state.db.execute("SELECT email FROM download_notifications").fetchall()
        assert [row["email"] for row in mails] == ["viewer@example.com"]
        radarr.unmonitor_movie.assert_not_called()

    def test_an_unmonitored_movie_that_has_its_file_is_left_alone_and_the_link_is_used(
        self, client, secret_key
    ):
        radarr = MagicMock()
        radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False, "hasFile": True}

        token, resp = self._post(client, secret_key, "movie", radarr, "build_radarr_from_db")

        assert resp.status_code == 409
        assert "already downloaded or on its way" in resp.json()["error"]
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
        assert "already downloaded or on its way" in resp.json()["error"]
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
        held = [{"tvdbId": 999}]
        sonarr.get_series.side_effect = [[], held, held]

        token, resp = self._post(client, secret_key, "tv", sonarr, "build_sonarr_from_db")

        sonarr.add_series.assert_called_once()
        assert resp.status_code == 409
        assert "already downloaded or on its way" in resp.json()["error"]
        assert _consumed(token)

    def test_a_400_for_a_series_sonarr_holds_but_will_not_fetch_is_already_exists_and_frees_the_link(
        self, client, secret_key
    ):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        sonarr.add_series.side_effect = SafeHTTPError(400, "already been added", "sonarr/s")
        held = [{"tvdbId": 999, "monitored": False, "seasons": []}]
        sonarr.get_series.side_effect = [[], held, held]

        token, resp = self._post(client, secret_key, "tv", sonarr, "build_sonarr_from_db")

        sonarr.add_series.assert_called_once()
        assert resp.status_code == 409
        assert "already downloaded or on its way" in resp.json()["error"]
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


class TestSubmitWhoseAddGotNoAnswer:
    """Radarr or Sonarr may add the title after mediaman stopped waiting for its answer."""

    NO_ANSWER = SafeHTTPError(0, "transport error: ReadTimeout", "arr/add")

    def _arr(self, media_type, held):
        arr = MagicMock()
        arr.get_movie_by_tmdb.side_effect = [None, {"id": 7, "monitored": True} if held else None]
        arr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        # Sonarr is asked before the add, when it holds nothing, and again after it.
        arr.get_series.side_effect = [[], [{"tvdbId": 999, "monitored": True}] if held else []]
        arr.add_movie.side_effect = arr.add_series.side_effect = self.NO_ANSWER
        return arr

    def _post(self, client, secret_key, media_type, arr):
        token = _token(secret_key, media_type)
        builder = "build_radarr_from_db" if media_type == "movie" else "build_sonarr_from_db"
        with patch(f"{_SUBMIT}.{builder}", return_value=arr):
            return token, client.post(f"/download/{token}")

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_an_add_that_landed_is_a_success_and_the_person_will_be_mailed(
        self, client, secret_key, media_type
    ):
        token, resp = self._post(client, secret_key, media_type, self._arr(media_type, held=True))

        assert resp.status_code == 200
        assert "poll_token" in resp.json()
        assert _consumed(token)
        mails = client.app.state.db.execute("SELECT email FROM download_notifications").fetchall()
        assert [m["email"] for m in mails] == ["viewer@example.com"]

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_an_add_that_did_not_land_is_a_502_and_frees_the_link(
        self, client, secret_key, media_type
    ):
        token, resp = self._post(client, secret_key, media_type, self._arr(media_type, held=False))

        assert resp.status_code == 502
        assert not _consumed(token)

    def test_a_show_sonarr_held_all_along_is_not_taken_for_an_add_that_landed(
        self, client, secret_key
    ):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        sonarr.get_series.return_value = [{"tvdbId": 999, "monitored": True}]
        sonarr.add_series.side_effect = self.NO_ANSWER

        token, resp = self._post(client, secret_key, "tv", sonarr)

        assert resp.status_code == 409
        sonarr.add_series.assert_not_called()
        mails = client.app.state.db.execute("SELECT email FROM download_notifications").fetchall()
        assert mails == []

    def test_a_show_whose_first_read_got_no_answer_is_a_502_and_frees_the_link(
        self, client, secret_key
    ):
        sonarr = MagicMock()
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        sonarr.get_series.side_effect = [
            self.NO_ANSWER,
            [{"tvdbId": 999, "monitored": True}],
        ]
        sonarr.add_series.side_effect = self.NO_ANSWER

        token, resp = self._post(client, secret_key, "tv", sonarr)

        assert resp.status_code == 502
        assert not _consumed(token)
        mails = client.app.state.db.execute("SELECT email FROM download_notifications").fetchall()
        assert mails == []


class TestRetryOfAnAddThatLandedLate:
    """The add got no answer, was not there yet when asked after, and landed later."""

    NO_ANSWER = SafeHTTPError(0, "transport error: ReadTimeout", "arr/add")

    def _arr(self, media_type):
        held = []
        arr = MagicMock(held=held)
        arr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        arr.get_movie_by_tmdb.side_effect = lambda _: held[0] if held else None
        arr.get_series.side_effect = lambda: list(held)
        arr.add_movie.side_effect = arr.add_series.side_effect = self.NO_ANSWER
        arr.landed = {"id": 7, "tmdbId": 500, "tvdbId": 999, "monitored": True}
        return arr

    def _post(self, client, token, media_type, arr):
        builder = "build_radarr_from_db" if media_type == "movie" else "build_sonarr_from_db"
        arr.get_movies.side_effect = lambda: list(arr.held)
        arr.get_queue.return_value = []
        with (
            patch(f"{_SUBMIT}.{builder}", return_value=arr),
            patch(f"{_CONFIRM}.{builder}", return_value=arr),
        ):
            return client.post(f"/download/{token}")

    def _mails(self, client):
        rows = client.app.state.db.execute("SELECT email FROM download_notifications")
        return [r["email"] for r in rows]

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_the_retry_is_a_success_and_the_person_will_be_mailed(
        self, client, secret_key, media_type
    ):
        arr, token = self._arr(media_type), _token(secret_key, media_type)
        assert self._post(client, token, media_type, arr).status_code == 502
        arr.held.append(arr.landed)
        reset_used_tokens()  # as after a restart

        resp = self._post(client, token, media_type, arr)

        assert resp.status_code == 200
        assert "poll_token" in resp.json()
        assert self._mails(client) == ["viewer@example.com"]
        assert arr.add_movie.call_count + arr.add_series.call_count == 1

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_the_reopened_page_promises_the_email_that_is_waiting(
        self, client, secret_key, media_type
    ):
        arr, token = self._arr(media_type), _token(secret_key, media_type)
        assert self._post(client, token, media_type, arr).status_code == 502
        landed = {500: {"tmdbId": 500, "monitored": True}}
        cache = {"radarr_movies": landed, "sonarr_series": landed}
        with (
            patch(f"{_CONFIRM}._get_sonarr_cache_cached", return_value=cache),
            patch(f"{_CONFIRM}._get_radarr_cache_cached", return_value=cache),
        ):
            page = client.get(f"/download/{token}").text

        assert '"download_state": "queued"' in page
        assert "send you a mail" in page
        assert self._mails(client) == ["viewer@example.com"]

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_the_email_waits_under_the_ids_the_title_lands_with(
        self, client, secret_key, media_type
    ):
        token = _token(secret_key, media_type)
        self._post(client, token, media_type, self._arr(media_type))

        row = client.app.state.db.execute(
            "SELECT service, media_type, tmdb_id, tvdb_id, notified FROM download_notifications"
        ).fetchone()
        wanted = {
            "movie": ("radarr", "movie", 500, None, 0),
            "tv": ("sonarr", "tv", 500, 999, 0),
        }
        assert tuple(row) == wanted[media_type]

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_a_retry_after_the_email_went_out_asks_for_no_second_one(
        self, client, secret_key, media_type
    ):
        arr, token = self._arr(media_type), _token(secret_key, media_type)
        self._post(client, token, media_type, arr)
        arr.held.append(arr.landed)
        conn = client.app.state.db
        with conn:
            conn.execute("UPDATE download_notifications SET notified = 1")

        assert self._post(client, token, media_type, arr).status_code == 200
        assert self._mails(client) == ["viewer@example.com"]

    def test_a_retry_the_database_fails_leaves_the_next_one_a_success(self, client, secret_key):
        arr, token = self._arr("movie"), _token(secret_key, "movie")
        self._post(client, token, "movie", arr)
        arr.held.append(arr.landed)
        conn = client.app.state.db
        conn.execute(
            "CREATE TRIGGER no_audit BEFORE INSERT ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
        assert self._post(client, token, "movie", arr).status_code == 502
        conn.execute("DROP TRIGGER no_audit")

        assert self._post(client, token, "movie", arr).status_code == 200
        assert self._mails(client) == ["viewer@example.com"]

    def test_an_email_that_cannot_be_kept_with_its_note_is_not_kept_at_all(
        self, client, secret_key
    ):
        conn = client.app.state.db
        conn.execute(
            "CREATE TRIGGER no_note BEFORE INSERT ON used_download_tokens "
            "WHEN (SELECT COUNT(*) FROM used_download_tokens) > 0 "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
        token = _token(secret_key, "movie")

        assert self._post(client, token, "movie", self._arr("movie")).status_code == 502
        assert not _consumed(token)
        assert self._mails(client) == []

    @pytest.mark.parametrize("media_type", ["movie", "tv"])
    def test_a_refused_retry_of_the_add_that_landed_meanwhile_is_a_success_too(
        self, client, secret_key, media_type
    ):
        arr, token = self._arr(media_type), _token(secret_key, media_type)
        assert self._post(client, token, media_type, arr).status_code == 502

        def refuse(*_):
            arr.held.append(arr.landed)
            raise SafeHTTPError(400, "already added", "arr/add")

        arr.add_movie.side_effect = arr.add_series.side_effect = refuse

        assert self._post(client, token, media_type, arr).status_code == 200
        assert self._mails(client) == ["viewer@example.com"]

    def test_another_persons_link_for_the_title_is_already_on_its_way_and_mails_them_too(
        self, client, secret_key
    ):
        arr = self._arr("movie")
        self._post(client, _token(secret_key, "movie"), "movie", arr)
        arr.held.append(arr.landed)
        other = generate_download_token(
            email="other@example.com",
            action="download",
            title="Dune",
            media_type="movie",
            tmdb_id=500,
            recommendation_id=None,
            secret_key=secret_key,
        )

        resp = self._post(client, other, "movie", arr)

        assert resp.status_code == 409
        assert resp.json()["download_state"] == "queued"
        assert self._mails(client) == ["viewer@example.com", "other@example.com"]

    def test_an_add_that_was_answered_with_a_refusal_is_no_late_landing(self, client, secret_key):
        arr, token = self._arr("movie"), _token(secret_key, "movie")
        arr.add_movie.side_effect = SafeHTTPError(400, "bad profile", "arr/add")
        assert self._post(client, token, "movie", arr).status_code == 502
        arr.held.append(arr.landed)

        # Held by now through somebody else: refused, and mailed once when it is ready.
        assert self._post(client, token, "movie", arr).status_code == 409
        assert self._mails(client) == ["viewer@example.com"]


class TestAlreadyThereAnswer:
    """What a refused request says of the title, and whether the person is mailed about it."""

    MOVIE = {"id": 7, "tmdbId": 500, "title": "Dune", "monitored": True}
    SHOW = {"id": 3, "tmdbId": 500, "tvdbId": 999, "title": "Far Shore", "monitored": True}
    MISSING = {"seasonNumber": 1, "monitored": True, "statistics": {"episodeCount": 2}}
    WHOLE = {
        "seasonNumber": 1,
        "monitored": True,
        "statistics": {
            "episodeCount": 2,
            "episodeFileCount": 2,
            "previousAiring": "2020-01-01T00:00:00Z",
        },
    }

    def _answer(self, client, token, held, media_type="movie", unread=None):
        arr = MagicMock()
        arr.get_movies.return_value = arr.get_series.return_value = [held]
        arr.get_movie_by_tmdb.return_value = arr.get_series_by_tvdb.return_value = held
        if unread:
            arr.get_movies.side_effect = unread
        arr.get_queue.return_value = []
        arr.lookup_by_tmdb_id.return_value = [{"tvdbId": 999}]
        builder = "build_radarr_from_db" if media_type == "movie" else "build_sonarr_from_db"
        with (
            patch(f"{_SUBMIT}.{builder}", return_value=arr),
            patch(f"{_CONFIRM}.{builder}", return_value=arr),
        ):
            return client.post(f"/download/{token}")

    def _post(self, client, token, held, media_type="movie"):
        resp = self._answer(client, token, held, media_type)
        assert resp.status_code == 409
        return resp.json()["download_state"]

    def _mails(self, client):
        rows = client.app.state.db.execute(
            "SELECT email, service, tmdb_id, tvdb_id FROM download_notifications"
        )
        return [tuple(r) for r in rows]

    def test_a_movie_on_its_way_mails_the_person(self, client, secret_key):
        assert self._post(client, _token(secret_key, "movie"), self.MOVIE) == "queued"
        assert self._mails(client) == [("viewer@example.com", "radarr", 500, None)]

    def test_a_show_on_its_way_mails_the_person(self, client, secret_key):
        held = {**self.SHOW, "seasons": [self.MISSING]}

        assert self._post(client, _token(secret_key, "tv"), held, "tv") == "queued"
        assert self._mails(client) == [("viewer@example.com", "sonarr", 500, 999)]

    def test_a_second_link_for_the_same_person_asks_for_no_second_email(self, client, secret_key):
        for recommendation in (1, 2):
            self._post(client, self._link(secret_key, recommendation), self.MOVIE)

        assert len(self._mails(client)) == 1

    def _link(self, secret_key, recommendation, action="download", media_type="movie"):
        return generate_download_token(
            email="viewer@example.com",
            action=action,
            title="Far Shore" if media_type == "tv" else "Dune",
            media_type=media_type,
            tmdb_id=500,
            recommendation_id=recommendation,
            secret_key=secret_key,
        )

    def _mark_sent(self, client, days_ago=0):
        asked = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        with client.app.state.db as conn:
            conn.execute("UPDATE download_notifications SET notified = 1, created_at = ?", (asked,))

    @pytest.mark.parametrize("action", ["download", "redownload"])
    def test_a_show_that_is_part_there_mails_the_person_once(self, client, secret_key, action):
        part = {"seasonNumber": 1, "monitored": True}
        part["statistics"] = {
            "episodeCount": 2,
            "episodeFileCount": 1,
            "previousAiring": "2020-01-01T00:00:00Z",
        }
        held = {**self.SHOW, "seasons": [part], "statistics": {"episodeFileCount": 1}}

        for number in range(3):
            answer = self._answer(
                client, self._link(secret_key, number, action, "tv"), held, "tv"
            ).json()
            assert (answer["download_state"], answer["notify_by_email"]) == ("queued", True)

        assert self._mails(client) == [("viewer@example.com", "sonarr", 500, 999)]

    @pytest.mark.parametrize("action", ["download", "redownload"])
    def test_a_wait_that_was_mailed_about_is_not_mailed_about_again(
        self, client, secret_key, action
    ):
        link = self._link(secret_key, 1, action)
        assert self._answer(client, link, self.MOVIE).json()["notify_by_email"] is True
        self._mark_sent(client, days_ago=1)

        for again in (2, 3):
            # A re-download link is freed by its 409 and can be pressed again itself.
            link = link if action == "redownload" else self._link(secret_key, again)
            assert self._answer(client, link, self.MOVIE).json()["notify_by_email"] is False

        assert len(self._mails(client)) == 1

    @pytest.mark.parametrize("since", ["long ago", "added again", "deleted"])
    def test_a_title_asked_for_anew_is_mailed_about_anew(self, client, secret_key, since):
        self._post(client, self._link(secret_key, 1), self.MOVIE)
        self._mark_sent(client, days_ago=60 if since == "long ago" else 2)
        held = dict(self.MOVIE)
        if since == "added again":
            held["added"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        if since == "deleted":
            with client.app.state.db as conn:
                audit_id = insert_audit_log(conn, media_item_id="gone", action="deleted")
                record_deleted_media(
                    conn,
                    audit_id,
                    media_type="movie",
                    tmdb_id=500,
                    tvdb_id=None,
                    season_number=None,
                    title="Dune",
                )

        answer = self._answer(client, self._link(secret_key, 2), held).json()

        assert answer["notify_by_email"] is True
        assert len(self._mails(client)) == 2

    @pytest.mark.parametrize("unread", [SafeHTTPError(500, "", "radarr/m"), ArrError("bad")])
    def test_a_title_whose_state_cannot_be_read_can_be_tried_again(
        self, client, secret_key, unread
    ):
        token = _token(secret_key, "movie")

        assert self._answer(client, token, self.MOVIE, unread=unread).status_code == 502
        assert not _consumed(token)
        assert self._post(client, token, self.MOVIE) == "queued"
        assert len(self._mails(client)) == 1

    def test_a_show_sonarr_knows_by_its_tvdb_id_alone_is_on_its_way_all_the_same(
        self, client, secret_key
    ):
        held = {**self.SHOW, "seasons": [self.MISSING]}
        del held["tmdbId"]
        token = _token(secret_key, "tv")

        assert self._post(client, token, held, "tv") == "queued"
        assert self._mails(client) == [("viewer@example.com", "sonarr", 500, 999)]
        reopened, polled = self._reopened_and_polled(client, token, held)
        assert '"download_state": "queued"' in reopened
        assert "send you a mail" in reopened
        assert polled == "ready"

    @pytest.mark.parametrize("was_held", [True, False], ids=["held", "added"])
    def test_a_show_known_by_its_tvdb_id_alone_shows_its_state_to_a_link_with_no_address(
        self, client, secret_key, was_held
    ):
        held = {**self.SHOW, "seasons": [self.MISSING]}
        del held["tmdbId"]
        token = _token(secret_key, "tv", email=None)
        if was_held:
            assert self._post(client, token, held, "tv") == "queued"
        else:
            added = self._answer(client, token, {**held, "tvdbId": 1}, "tv")
            assert added.status_code == 200

        reopened, polled = self._reopened_and_polled(client, token, held)

        assert '"download_state": "queued"' in reopened
        assert "send you a mail" not in reopened
        assert polled == "ready"

    def test_a_refused_link_with_no_address_is_no_request_that_keeps_a_deleted_season_monitored(
        self, client, secret_key
    ):
        conn = client.app.state.db
        audit_id = insert_audit_log(
            conn,
            media_item_id="gone",
            action="deleted",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        held = {**self.SHOW, "seasons": [self.MISSING]}
        del held["tmdbId"]

        self._post(client, _token(secret_key, "tv", email=None), held, "tv")

        gone = DeletionRow(
            id=audit_id,
            media_item_id="gone",
            action="deleted",
            file_path=None,
            file_size_bytes=None,
            title="Far Shore",
            plex_rating_key=None,
            radarr_id=None,
            sonarr_id=None,
            season_number=1,
            media_type="tv_season",
            tmdb_id=500,
            tvdb_id=999,
        )
        assert not was_requested_since(conn, gone)

    def _reopened_and_polled(self, client, token, held):
        sonarr = MagicMock()
        sonarr.get_series.return_value = [held]
        sonarr.get_series_by_tvdb.side_effect = lambda tvdb: held if tvdb == 999 else None
        sonarr.get_queue.return_value = []
        with patch(f"{_CONFIRM}.build_sonarr_from_db", return_value=sonarr):
            reopened = client.get(f"/download/{token}").text
        found = re.search(r'"poll_token": ("[^"]+")', reopened)
        if found is None:
            return reopened, None
        poll = json.loads(found[1])
        sonarr.get_series.return_value = [{**held, "seasons": [self.WHOLE]}]
        status = "mediaman.web.routes.download.status.build_sonarr_from_db"
        with patch(status, return_value=sonarr):
            polled = client.get(
                "/api/download/status",
                params={"service": "sonarr", "tmdb_id": 500, "poll_token": poll},
            )
        return reopened, polled.json()["state"]

    @pytest.mark.parametrize(
        ("media_type", "held", "state"),
        [
            ("movie", {**MOVIE, "hasFile": True}, "in_library"),
            ("tv", {**SHOW, "seasons": [WHOLE]}, "in_library"),
            ("tv", {**SHOW, "monitored": False, "seasons": [MISSING]}, "tracked"),
        ],
    )
    def test_a_title_that_is_not_on_its_way_mails_nobody(
        self, client, secret_key, media_type, held, state
    ):
        assert self._post(client, _token(secret_key, media_type), held, media_type) == state
        assert self._mails(client) == []
