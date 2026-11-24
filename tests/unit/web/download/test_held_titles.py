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
        held = [{"tvdbId": 999}]
        sonarr.get_series.side_effect = [[], held, held]

        token, resp = self._post(client, secret_key, "tv", sonarr, "build_sonarr_from_db")

        sonarr.add_series.assert_called_once()
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
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
        arr.landed = {"id": 7, "tvdbId": 999, "monitored": True}
        return arr

    def _post(self, client, token, media_type, arr):
        builder = "build_radarr_from_db" if media_type == "movie" else "build_sonarr_from_db"
        with patch(f"{_SUBMIT}.{builder}", return_value=arr):
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
        assert "notified by email" in page
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

    def test_another_link_for_the_title_is_already_exists(self, client, secret_key):
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

        assert self._post(client, other, "movie", arr).status_code == 409
        assert self._mails(client) == ["viewer@example.com"]

    def test_an_add_that_was_answered_with_a_refusal_is_no_late_landing(self, client, secret_key):
        arr, token = self._arr("movie"), _token(secret_key, "movie")
        arr.add_movie.side_effect = SafeHTTPError(400, "bad profile", "arr/add")
        assert self._post(client, token, "movie", arr).status_code == 502
        arr.held.append(arr.landed)

        assert self._post(client, token, "movie", arr).status_code == 409
        assert self._mails(client) == []
