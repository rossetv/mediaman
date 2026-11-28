"""A ready mail for a title that was never a pick takes its details from TMDB, then from
Sonarr's entry for a show; with neither it still goes out, with fewer blocks."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
from mediaman.services.downloads._notification_email import (
    SuggestionsMeta,
    build_email_payload,
    get_notification_template,
)
from mediaman.services.media_meta.tmdb import TmdbClient
from tests.helpers.factories import insert_download_notification, insert_settings

SECRET = "s" * 64
SITE = "https://mm.example"
TOKEN = "tmdb-read-token-secret"

ALONE = {
    "name": "Alone",
    "first_air_date": "2015-06-18",
    "overview": "Survivalists are dropped in the wild with no one but themselves.",
    "poster_path": "/alonePoster.jpg",
    "backdrop_path": "/aloneWide.jpg",
    "vote_average": 7.4,
    "vote_count": 300,
    "episode_run_time": [44],
    "created_by": [{"id": 1, "name": "Shaw Daly"}],
    "number_of_seasons": 11,
}

# Sonarr's pictures are TheTVDB's.
SONARR_POSTER = "https://artworks.thetvdb.com/banners/v4/series/295604/posters/62d1b6c8a9c35.jpg"
SONARR_WIDE = "https://artworks.thetvdb.com/banners/fanart/original/295604-3.jpg"

SERIES = {
    "title": "Alone",
    "tvdbId": 9,
    "tmdbId": 5,
    "year": 2015,
    "overview": "Sonarr's own words about the show.",
    "images": [
        {
            "coverType": "poster",
            "remoteUrl": SONARR_POSTER + "?apikey=SONARRKEY",
            "url": "/MediaCover/1/poster.jpg?apikey=SONARRKEY",
        },
        {"coverType": "fanart", "remoteUrl": SONARR_WIDE},
    ],
}


@pytest.fixture
def conn(tmp_path):
    db = init_db(str(tmp_path / "mm.db"))
    insert_settings(db, tmdb_read_token=TOKEN)
    return db


@pytest.fixture
def tmdb(monkeypatch):
    """TMDB's details answer, as the test sets it; never the real TMDB."""
    fake = MagicMock(return_value=ALONE)
    monkeypatch.setattr(TmdbClient, "details_or_raise", lambda self, kind, tid: fake(kind, tid))
    return fake


def _row(**fields):
    values = {
        "id": 1,
        "email": "u@x",
        "title": "Alone",
        "media_type": "tv",
        "tmdb_id": 5,
        "tvdb_id": 9,
        "service": "sonarr",
        "claimed_at": "",
        "season_number": None,
    }
    return ClaimedNotificationRow(**(values | fields))


def _html(conn, row=None, *, movie=None, picks=None, series=None):
    return build_email_payload(
        row or _row(),
        movie,
        picks or {},
        get_notification_template(),
        SITE,
        SECRET,
        conn=conn,
        series=series,
    )[1]


def test_a_show_never_picked_takes_its_pictures_year_and_words_from_tmdb(conn, tmdb):
    html = _html(conn, series=SERIES)

    assert "/api/img/tmdb/w1280/aloneWide.jpg?sig=" in html
    assert "2015" in html
    assert ALONE["overview"] in html
    assert "Shaw Daly" in html
    assert "7.4" in html
    assert "thetvdb" not in html and "Sonarr&#39;s own words" not in html
    tmdb.assert_called_once_with("tv", 5)


def test_a_show_with_no_wide_picture_shows_tmdbs_poster(conn, tmdb):
    tmdb.return_value = ALONE | {"backdrop_path": None}

    html = _html(conn)

    assert "/api/img/tmdb/w500/alonePoster.jpg?sig=" in html


def test_with_tmdb_failing_the_show_takes_sonarrs_entry(conn, tmdb):
    tmdb.side_effect = requests.ConnectionError("down")

    html = _html(conn, series=SERIES)

    assert f'src="{SONARR_WIDE}"' in html
    assert "2015" in html
    assert "Sonarr&#39;s own words about the show." in html


def test_with_no_tmdb_key_the_show_has_sonarrs_pictures(tmp_path, tmdb):
    bare = init_db(str(tmp_path / "bare.db"))
    no_wide = SERIES | {"images": [{"coverType": "poster", "remoteUrl": SONARR_POSTER}]}

    assert f'src="{SONARR_WIDE}"' in _html(bare, series=SERIES)
    assert f'src="{SONARR_POSTER}"' in _html(bare, series=no_wide)
    tmdb.assert_not_called()


@pytest.mark.parametrize(
    "remote",
    [
        SONARR_WIDE.replace("https://", "http://"),
        SONARR_WIDE.replace("artworks.thetvdb.com", "artworks.thetvdb.com.evil.example"),
        "https://thetvdb.com/banners/fanart/original/295604-3.jpg",
        "https://artworks.thetvdb.com/banners/fanart/original/295604-3.svg",
    ],
)
def test_a_picture_sonarr_names_elsewhere_is_not_put_in_the_mail(tmp_path, tmdb, remote):
    series = SERIES | {"images": [{"coverType": "fanart", "remoteUrl": remote}]}

    html = _html(init_db(str(tmp_path / "bare.db")), series=series)

    assert '<img class="pic"' not in html and "295604" not in html


def test_a_row_with_only_a_tvdb_id_asks_tmdb_by_the_series_tmdb_id(conn, tmdb):
    _html(conn, _row(tmdb_id=None), series=SERIES)

    tmdb.assert_called_once_with("tv", 5)


def test_with_tmdb_and_sonarr_failing_the_mail_still_has_its_title(conn, tmdb):
    tmdb.side_effect = requests.Timeout("slow")

    html = _html(conn)

    assert "Alone" in html and "Ready to watch" in html
    assert '<img class="pic"' not in html
    assert "watch#title=tmdb-tv-5" in html


def test_with_tmdb_answering_garbage_the_mail_still_goes_out(conn, tmdb):
    tmdb.side_effect = RuntimeError("unexpected")

    assert "Alone" in _html(conn)


def test_no_key_or_token_is_in_the_mail(conn, tmdb):
    for html in (_html(conn, series=SERIES), _html(conn, series=SERIES | {"tmdbId": None})):
        assert TOKEN not in html
        assert SECRET not in html
        assert "SONARRKEY" not in html
        assert "apikey" not in html.lower() and "api_key" not in html.lower()


def test_a_pick_is_not_looked_up_on_tmdb(conn, tmdb):
    pick = SuggestionsMeta(
        tmdb_id=5,
        year="2016",
        runtime="",
        director="",
        description="From the picks.",
        rating="",
        imdb_rating="",
        rt_rating="",
        poster_url="https://image.tmdb.org/t/p/w300/pick.jpg",
    )

    html = _html(conn, picks={5: pick}, series=SERIES)

    assert "From the picks." in html and "2016" in html
    assert "/api/img/tmdb/w500/pick.jpg?sig=" in html
    tmdb.assert_not_called()


def test_a_film_with_no_tmdb_set_up_keeps_radarrs_poster(tmp_path, tmdb):
    film = _row(title="Dune", media_type="movie", tmdb_id=42, tvdb_id=None, service="radarr")
    movie = {
        "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/t/p/w300/d.jpg"}]
    }

    html = _html(init_db(str(tmp_path / "bare.db")), film, movie=movie)

    assert "/api/img/tmdb/w500/d.jpg?sig=" in html
    tmdb.assert_not_called()


def test_the_scheduled_mail_of_a_show_carries_tmdbs_details(conn, tmdb, monkeypatch):
    insert_settings(
        conn,
        mailgun_domain="test.example.com",
        mailgun_api_key="dummy-key",
        mailgun_from_address="noreply@test.example.com",
        base_url=SITE,
    )
    insert_download_notification(
        conn,
        email="user@example.com",
        title="Alone",
        media_type="tv",
        tmdb_id=5,
        tvdb_id=9,
        service="sonarr",
    )
    sonarr, mailgun = MagicMock(), MagicMock()
    ready = {"statistics": {"episodeFileCount": 10, "episodeCount": 10, "totalEpisodeCount": 10}}
    sonarr.get_series.return_value = [SERIES | ready]
    monkeypatch.setattr("mediaman.services.downloads.notifications.series_ready", lambda s, n: True)
    for name, value in (
        ("arr.build.build_radarr_from_db", None),
        ("arr.build.build_sonarr_from_db", sonarr),
        ("mail.mailgun.MailgunClient", mailgun),
    ):
        monkeypatch.setattr(f"mediaman.services.{name}", lambda *a, _v=value, **kw: _v)
    from mediaman.services.downloads.notifications import check_download_notifications

    check_download_notifications(conn, secret_key=SECRET)

    html = mailgun.send.call_args.kwargs["html"]
    assert ALONE["overview"] in html and "aloneWide.jpg" in html
