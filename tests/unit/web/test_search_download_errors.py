"""A failed add must not block an immediate retry, and Arr errors must not become 500s."""

# ruff: noqa: F811

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from mediaman.services.arr import ArrError
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import set_user_email
from mediaman.web.routes.download import confirm
from mediaman.web.routes.search import download as dl
from tests.unit.web.test_search_download_rate_limit import (  # noqa: F401
    _setup_limiters,
    _valid_body,
    app,
    authed_client,
)

_MODULE = "mediaman.web.routes.search.download"


@pytest.fixture(autouse=True)
def _reset():
    _setup_limiters()


def test_failed_add_does_not_block_immediate_retry(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    radarr.add_movie.side_effect = [ArrError("boom"), None]
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr):
        first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=7))
        second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=7))
    assert first.status_code == 502
    assert second.status_code == 200


def test_successful_add_still_suppresses_duplicate(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr):
        first = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=8))
        second = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=8))
    assert first.status_code == 200
    assert second.status_code == 429


def test_raising_submit_releases_the_dedup_key(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with (
        patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr),
        patch(
            f"{_MODULE}._submit_movie",
            side_effect=[RuntimeError("boom"), JSONResponse({"ok": True})],
        ),
    ):
        with pytest.raises(RuntimeError):
            authed_client.post("/api/search/download", json=_valid_body(tmdb_id=9))
        retry = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=9))
    assert retry.status_code == 200


def test_failing_admin_email_lookup_releases_the_dedup_key(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    with (
        patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr),
        patch(f"{_MODULE}._resolve_admin_email", side_effect=[RuntimeError("db"), None]),
    ):
        with pytest.raises(RuntimeError):
            authed_client.post("/api/search/download", json=_valid_body(tmdb_id=10))
        retry = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=10))
    assert retry.status_code == 200


def test_arr_error_in_tvdb_lookup_is_a_plain_502(conn):
    sonarr = MagicMock()
    sonarr.lookup_series_by_tmdb.side_effect = ArrError("secret internal detail")
    body = MagicMock(tmdb_id=5, title="Show")
    with patch(f"{_MODULE}.build_sonarr_from_db", return_value=sonarr):
        resp = dl._submit_tv(conn, "k", body, None, "admin")
    assert resp.status_code == 502
    assert b"secret internal detail" not in resp.body


def test_arr_error_in_tracked_check_is_a_plain_503(conn):
    sonarr = MagicMock()
    sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 10}
    body = MagicMock(tmdb_id=5, title="Show")
    with (
        patch(f"{_MODULE}.build_sonarr_from_db", return_value=sonarr),
        patch(f"{_MODULE}.is_series_already_tracked", side_effect=ArrError("secret")),
    ):
        resp = dl._submit_tv(conn, "k", body, None, "admin")
    assert resp.status_code == 503
    assert b"secret" not in resp.body


def test_radarr_not_configured_sends_its_sentence_as_message(authed_client):
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=None):
        resp = authed_client.post("/api/search/download", json=_valid_body(tmdb_id=11))
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "Radarr not configured"
    assert body["message"] == body["error"]


_NO_ANSWER = SafeHTTPError(0, "transport error: ReadTimeout", "arr/add")


def _post_with_an_email(authed_client, app, builder, arr, body):
    set_user_email(app.state.db, "admin", "admin@example.com")
    with patch(f"{_MODULE}.{builder}", return_value=arr):
        return authed_client.post("/api/search/download", json=body)


def _mailed(app):
    rows = app.state.db.execute("SELECT email, username FROM download_notifications").fetchall()
    assert {r["username"] for r in rows} <= {"admin"}
    return [r["email"] for r in rows]


@pytest.mark.parametrize("held", [{"id": 7, "monitored": True}, None])
def test_a_movie_add_with_no_answer_is_a_success_only_when_radarr_has_the_movie(
    authed_client, app, held
):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.side_effect = [None, held]
    radarr.add_movie.side_effect = _NO_ANSWER

    resp = _post_with_an_email(
        authed_client, app, "build_radarr_from_db", radarr, _valid_body(tmdb_id=12)
    )

    assert resp.status_code == (200 if held else 502)
    assert _mailed(app) == (["admin@example.com"] if held else [])


@pytest.mark.parametrize("seasons", [None, [1]])
def test_a_series_add_with_no_answer_that_landed_is_a_success(authed_client, app, seasons):
    sonarr = MagicMock()
    sonarr.lookup_series_by_tmdb.return_value = {"tvdbId": 999}
    sonarr.get_series.side_effect = [[], [{"tvdbId": 999, "monitored": True}]]
    sonarr.add_series.side_effect = sonarr.add_series_with_seasons.side_effect = _NO_ANSWER

    resp = _post_with_an_email(
        authed_client,
        app,
        "build_sonarr_from_db",
        sonarr,
        {**_valid_body("tv", 13), "monitored_seasons": seasons},
    )

    assert resp.status_code == 200
    assert _mailed(app) == ["admin@example.com"]


def test_a_failed_re_monitor_of_a_filed_movie_is_still_a_failure(authed_client, app):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = {"id": 7, "monitored": False, "hasFile": True}
    radarr.remonitor_movie.side_effect = _NO_ANSWER

    resp = _post_with_an_email(
        authed_client, app, "build_radarr_from_db", radarr, _valid_body(tmdb_id=14)
    )

    assert resp.status_code == 502
    assert _mailed(app) == []


def test_the_emailed_page_forgets_its_arr_list_when_search_adds_a_title(authed_client):
    radarr = MagicMock()
    radarr.get_movie_by_tmdb.return_value = None
    confirm._RADARR_CACHE["key"] = (0.0, {"radarr_movies": {}})
    with patch(f"{_MODULE}.build_radarr_from_db", return_value=radarr):
        authed_client.post("/api/search/download", json=_valid_body(tmdb_id=15))

    assert confirm._RADARR_CACHE == {}
