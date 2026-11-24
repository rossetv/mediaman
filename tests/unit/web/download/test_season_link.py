"""A newsletter's Re-Download link for a deleted season fetches that season and no other."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from mediaman.core.deleted_media import record_deleted_media
from mediaman.core.time import now_utc
from mediaman.crypto import generate_download_token, validate_poll_token
from mediaman.db import init_db, set_connection
from mediaman.main import create_app
from mediaman.scanner.arr_dates import ArrDateCache
from mediaman.scanner.fetch import PlexItemFetch
from mediaman.scanner.phases.upsert import upsert_item
from mediaman.services.infra import SafeHTTPError
from mediaman.services.mail.newsletter.subscribers import _mint_deleted_tokens
from mediaman.services.mail.newsletter.summary import _load_deleted_items
from mediaman.services.media_meta._plex_types import _season_to_item
from mediaman.web.repository.library_api import snapshot_media_for_delete
from mediaman.web.routes.download import (
    reset_download_caches,
    reset_download_limiters,
    reset_used_tokens,
)
from tests.helpers.factories import insert_audit_log

_ROUTES = "mediaman.web.routes.download"
_TMDB, _TVDB = 60622, 269613


@pytest.fixture
def client(db_path, secret_key):
    conn = init_db(str(db_path))
    set_connection(conn)
    app = create_app()
    app.state.config = MagicMock(secret_key=secret_key, data_dir=str(db_path.parent))
    app.state.db = conn
    reset_download_caches()
    reset_download_limiters()
    reset_used_tokens()
    yield TestClient(app)
    conn.close()


def _delete_season(conn, number):
    _record_deletion(conn, number, _TMDB, _TVDB)


def _record_deletion(conn, number, tmdb_id, tvdb_id):
    audit_id = insert_audit_log(
        conn,
        media_item_id=f"gone{number}",
        action="deleted",
        detail="Deleted: Fargo",
        created_at=now_utc() - timedelta(days=1),
    )
    record_deleted_media(
        conn,
        audit_id,
        media_type="tv_season",
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        season_number=number,
        title="Fargo",
    )
    conn.commit()


def _scan_and_delete_season(conn, guids):
    """Season 2 as Plex lists it, through the scan's upsert, then deleted with the ids it stored."""
    show = SimpleNamespace(
        ratingKey=10,
        title="Fargo",
        updatedAt=None,
        thumb="/thumb",
        guid="plex://show/5d9c",
        guids=[SimpleNamespace(id=g) for g in guids],
        genres=[],
        studio=None,
        librarySectionTitle="TV Shows",
    )
    season = SimpleNamespace(ratingKey=12, index=2, addedAt=None, episodes=list)
    fetched = PlexItemFetch(_season_to_item(show, season), "1", "tv_season", [])
    with conn:
        upsert_item(conn, fetched, ArrDateCache(), "tv_season")
    gone = snapshot_media_for_delete(conn, "12")
    with conn:
        conn.execute("DELETE FROM media_items WHERE id = '12'")
    _record_deletion(conn, gone.season_number, gone.tmdb_id, gone.tvdb_id)


def _cards(conn, secret_key):
    cards = _load_deleted_items(conn, secret_key, "http://mediaman.local", now_utc())
    _mint_deleted_tokens(
        cards, email="a@example.com", base_url="http://mediaman.local", secret_key=secret_key
    )
    return {c["season_number"]: urlsplit(c["redownload_url"]).path for c in cards}


def _series(season_two, **extra):
    full = {"episodeFileCount": 8, "episodeCount": 8, "previousAiring": "2026"}
    return {
        "id": 3,
        "tmdbId": _TMDB,
        "tvdbId": _TVDB,
        "title": "Fargo",
        "monitored": True,
        "statistics": {"episodeFileCount": 8},
        "seasons": [{"seasonNumber": 1, "monitored": True, "statistics": full}, season_two],
        **extra,
    }


UNMONITORED = {"seasonNumber": 2, "monitored": False, "statistics": {}}
MONITORED = {"seasonNumber": 2, "monitored": True, "statistics": {}}
ON_DISK = {
    "seasonNumber": 2,
    "monitored": True,
    "statistics": {"episodeFileCount": 8, "episodeCount": 8},
}


#: Three episodes left of eight: an unmonitored episode with no file is not counted.
PART_LEFT = {
    "seasonNumber": 2,
    "monitored": False,
    "statistics": {"episodeFileCount": 3, "episodeCount": 3, "totalEpisodeCount": 8},
}


def _sonarr(series):
    sonarr = MagicMock()
    sonarr.get_series_by_tvdb.return_value = series
    sonarr.get_series.return_value = [series] if series else []
    sonarr.get_queue.return_value = []
    return sonarr


def _post(client, url, sonarr):
    with patch(f"{_ROUTES}.submit.build_sonarr_from_db", return_value=sonarr):
        return client.post(url)


class TestSubmit:
    @pytest.fixture
    def link(self, client, secret_key):
        _delete_season(client.app.state.db, 1)
        _delete_season(client.app.state.db, 2)
        return _cards(client.app.state.db, secret_key)[2]

    def test_a_season_of_a_show_sonarr_holds_is_re_monitored(self, client, link):
        sonarr = _sonarr(_series(UNMONITORED))

        resp = _post(client, link, sonarr)

        assert resp.status_code == 200
        sonarr.remonitor_season.assert_called_once_with(3, 2)
        sonarr.add_series.assert_not_called()
        sonarr.add_series_with_seasons.assert_not_called()

    def test_a_season_already_complete_is_already_there_and_frees_the_link(
        self, client, link, secret_key
    ):
        sonarr = _sonarr(_series(ON_DISK))

        resp = _post(client, link, sonarr)

        assert resp.status_code == 409
        assert "Season 2" in resp.json()["error"]
        assert validate_poll_token(resp.json()["poll_token"], secret_key)["mid"] == (
            "sonarr-season:2:Fargo"
        )
        sonarr.remonitor_season.assert_not_called()
        assert _post(client, link, _sonarr(_series(UNMONITORED))).status_code == 200

    def test_a_season_whose_add_landed_late_and_is_complete_by_the_retry_is_a_success(
        self, client, link
    ):
        sonarr = _sonarr(None)
        sonarr.add_series_with_seasons.side_effect = SafeHTTPError(0, "timeout", "sonarr/s")
        assert _post(client, link, sonarr).status_code == 502
        sonarr.get_series_by_tvdb.return_value = _series(ON_DISK)

        assert _post(client, link, sonarr).status_code == 200
        mails = client.app.state.db.execute("SELECT email FROM download_notifications").fetchall()
        assert [m["email"] for m in mails] == ["a@example.com"]

    def test_a_season_with_some_episodes_left_is_fetched(self, client, link):
        sonarr = _sonarr(_series(PART_LEFT))

        assert _post(client, link, sonarr).status_code == 200
        sonarr.remonitor_season.assert_called_once_with(3, 2)

    def test_a_show_sonarr_no_longer_holds_is_added_with_that_season_alone(self, client, link):
        sonarr = _sonarr(None)

        resp = _post(client, link, sonarr)

        assert resp.status_code == 200
        sonarr.add_series_with_seasons.assert_called_once_with(_TVDB, "Fargo", [2])
        sonarr.add_series.assert_not_called()

    def test_the_email_waits_for_the_season_and_only_its_card_leaves_the_next_newsletter(
        self, client, link, secret_key
    ):
        _post(client, link, _sonarr(_series(UNMONITORED)))

        conn = client.app.state.db
        note = conn.execute("SELECT tvdb_id, season_number FROM download_notifications").fetchone()
        assert tuple(note) == (_TVDB, 2)
        assert set(_cards(conn, secret_key)) == {1}

    @pytest.mark.parametrize(("season_two", "put_back"), [(UNMONITORED, 1), (MONITORED, 0)])
    def test_a_failed_search_frees_the_link_and_puts_back_what_it_changed(
        self, client, link, season_two, put_back
    ):
        sonarr = _sonarr(_series(season_two))
        sonarr.remonitor_season.side_effect = [SafeHTTPError(409, "busy", "sonarr/c"), None]

        assert _post(client, link, sonarr).status_code == 502
        assert sonarr.unmonitor_season.call_count == put_back
        assert _post(client, link, sonarr).status_code == 200

    def test_a_held_show_with_no_id_is_a_502_never_added_again_and_frees_the_link(
        self, client, link
    ):
        sonarr = _sonarr(_series(UNMONITORED, id=None))
        sonarr.add_series_with_seasons.side_effect = SafeHTTPError(0, "timeout", "sonarr/s")

        assert _post(client, link, sonarr).status_code == 502
        sonarr.get_series_by_tvdb.return_value = _series(UNMONITORED)
        assert _post(client, link, sonarr).status_code == 200
        sonarr.add_series_with_seasons.assert_not_called()

    def test_a_link_from_before_seasons_were_named_still_adds_the_show(self, client, secret_key):
        _delete_season(client.app.state.db, 2)
        token = generate_download_token(
            email="a@example.com",
            action="redownload",
            title="Fargo",
            media_type="tv",
            tmdb_id=_TMDB,
            recommendation_id=None,
            secret_key=secret_key,
        )
        sonarr = _sonarr(None)
        sonarr.lookup_by_tmdb_id.return_value = [{"tvdbId": _TVDB}]

        assert _post(client, f"/download/{token}", sonarr).status_code == 200
        sonarr.add_series.assert_called_once_with(_TVDB, "Fargo")


class TestScannedSeason:
    """A season carries no TMDB id of its own; the link needs its show's."""

    def _tmdb(self, **find):
        return patch(
            "mediaman.services.media_meta.tmdb.TmdbClient.from_db",
            return_value=MagicMock(find_by_tvdb=MagicMock(**find)),
        )

    def test_the_link_of_a_scanned_season_fetches_that_season(self, client, secret_key):
        conn = client.app.state.db
        _scan_and_delete_season(conn, [f"tmdb://{_TMDB}", f"tvdb://{_TVDB}"])
        sonarr = _sonarr(_series(UNMONITORED))

        assert _post(client, _cards(conn, secret_key)[2], sonarr).status_code == 200
        sonarr.remonitor_season.assert_called_once_with(3, 2)

    def test_a_season_recorded_without_one_gets_it_from_its_tvdb_id(self, client, secret_key):
        conn = client.app.state.db
        _scan_and_delete_season(conn, [f"tvdb://{_TVDB}"])
        sonarr = _sonarr(_series(UNMONITORED))

        with self._tmdb(return_value=_TMDB) as from_db:
            link = _cards(conn, secret_key)[2]
            _cards(conn, secret_key)

        from_db.return_value.find_by_tvdb.assert_called_once_with(_TVDB)
        assert _post(client, link, sonarr).status_code == 200
        sonarr.remonitor_season.assert_called_once_with(3, 2)

    @pytest.mark.parametrize(
        "find", [{"return_value": None}, {"side_effect": SafeHTTPError(0, "timeout", "tmdb/find")}]
    )
    def test_a_season_tmdb_cannot_name_has_no_link_and_the_newsletter_still_goes(
        self, client, secret_key, find
    ):
        conn = client.app.state.db
        _scan_and_delete_season(conn, [f"tvdb://{_TVDB}"])

        with self._tmdb(**find):
            assert _cards(conn, secret_key) == {2: ""}


class TestPage:
    @pytest.mark.parametrize(
        ("season_two", "state"),
        [
            (UNMONITORED, ""),
            (PART_LEFT, ""),
            (MONITORED, "queued"),
            (ON_DISK, "in_library"),
        ],
    )
    def test_it_goes_by_the_season_not_the_rest_of_the_show(
        self, client, secret_key, season_two, state
    ):
        _delete_season(client.app.state.db, 2)
        link = _cards(client.app.state.db, secret_key)[2]
        with patch(
            f"{_ROUTES}.confirm.build_sonarr_from_db", return_value=_sonarr(_series(season_two))
        ):
            page = client.get(link).text

        assert f'"download_state": "{state}"' in page
        assert ("Re-Download Season 2" in page) == (state == "")


class TestProgress:
    @pytest.mark.parametrize(
        ("season_two", "state"), [(MONITORED, "searching"), (ON_DISK, "ready")]
    )
    def test_it_is_ready_when_the_season_is_not_when_another_one_is(
        self, client, secret_key, season_two, state
    ):
        _delete_season(client.app.state.db, 2)
        link = _cards(client.app.state.db, secret_key)[2]
        poll_token = _post(client, link, _sonarr(_series(UNMONITORED))).json()["poll_token"]
        sonarr = _sonarr(_series(season_two))
        other_season = {
            "series": {"tmdbId": _TMDB, "title": "Fargo"},
            "episode": {"seasonNumber": 1, "episodeNumber": 1},
            "size": 10,
            "sizeleft": 5,
            "status": "downloading",
        }
        sonarr.get_queue.return_value = [other_season]

        assert validate_poll_token(poll_token, secret_key)["svc"] == "sonarr"
        with patch(f"{_ROUTES}.status.build_sonarr_from_db", return_value=sonarr):
            status = client.get(
                "/api/download/status",
                params={"service": "sonarr", "tmdb_id": _TMDB, "poll_token": poll_token},
            ).json()

        assert status["state"] == state
