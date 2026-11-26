"""The ready mail's and the newsletter picks' TMDB pictures come from this site's signed address
when the site address is known, and stay as they are when it is not."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

import pytest

from mediaman.crypto import validate_tmdb_image_token
from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
from mediaman.services.downloads._notification_email import (
    SuggestionsMeta,
    build_email_payload,
    get_notification_template,
)
from mediaman.services.mail.newsletter.summary import _load_recommendations, load_own_picks
from tests.helpers.factories import insert_admin_user, insert_suggestion, insert_user_pick

SITE = "https://media.example"
POSTER = "https://image.tmdb.org/t/p/w300/p.jpg"
WIDE = "https://image.tmdb.org/t/p/w1280/wide.jpg"
DAY = 86400


def _ready_mail(
    secret_key: str, base_url: str, poster: str = POSTER, wide: str = WIDE, movie=None
) -> str:
    row = ClaimedNotificationRow(
        id=1,
        email="u@example.com",
        title="Dune",
        media_type="movie",
        tmdb_id=42,
        tvdb_id=None,
        service="radarr",
        claimed_at="",
    )
    meta = SuggestionsMeta(
        tmdb_id=42,
        year="2021",
        runtime="",
        director="",
        description="",
        rating="",
        imdb_rating="",
        rt_rating="",
        poster_url=poster,
        backdrop_url=wide,
    )
    _subject, html = build_email_payload(
        row,
        movie,
        {42: meta} if poster else {},
        get_notification_template(),
        base_url,
        secret_key,
    )
    return html


def _pictures(html: str) -> set[str]:
    return set(re.findall(r'<img [^>]*src="([^"]*)"', html))


def _lasts_days(url: str, secret_key: str) -> float:
    payload = validate_tmdb_image_token(url.partition("?sig=")[2], secret_key)
    assert payload is not None
    return (payload["exp"] - time.time()) / DAY


@pytest.mark.parametrize(
    ("wide", "shown"), [(WIDE, "/api/img/tmdb/w1280/wide.jpg"), ("", "/api/img/tmdb/w500/p.jpg")]
)
def test_the_ready_mail_takes_tmdbs_pictures_from_this_site(secret_key, wide, shown):
    html = _ready_mail(secret_key, SITE, wide=wide)

    pictures = _pictures(html)
    assert {p.split("?")[0] for p in pictures} == {SITE + shown}
    assert "image.tmdb.org" not in html
    assert all(180 <= _lasts_days(p, secret_key) <= 181 for p in pictures)


@pytest.mark.parametrize(
    ("wide", "shown"), [(WIDE, WIDE), ("", "https://image.tmdb.org/t/p/w500/p.jpg")]
)
def test_with_no_site_address_the_ready_mail_s_pictures_are_tmdbs(secret_key, wide, shown):
    assert _pictures(_ready_mail(secret_key, "", wide=wide)) == {shown}


def test_a_poster_radarr_names_on_another_host_is_left_as_it_is(secret_key):
    remote = "https://mediacover.radarr.video/p.jpg"
    movie = {"images": [{"coverType": "poster", "remoteUrl": remote}]}

    html = _ready_mail(secret_key, SITE, poster="", wide="", movie=movie)

    assert remote in _pictures(html)


@pytest.fixture
def shared_pick(conn):
    insert_suggestion(conn, title="Dune", tmdb_id=42, poster_url=POSTER, batch_id="2026-11-20")


def test_a_shared_pick_s_poster_comes_from_this_site(conn, secret_key, shared_pick):
    (pick,) = _load_recommendations(conn, base_url=SITE, secret_key=secret_key)

    assert pick["poster_url"]
    assert pick["poster_url"].startswith(f"{SITE}/api/img/tmdb/w342/p.jpg?sig=")
    assert 180 <= _lasts_days(pick["poster_url"], secret_key) <= 181


def test_with_no_site_address_a_pick_s_poster_is_the_stored_one(conn, secret_key, shared_pick):
    (pick,) = _load_recommendations(conn, secret_key=secret_key)

    assert pick["poster_url"] == POSTER


def test_an_own_pick_s_poster_comes_from_this_site(conn, secret_key):
    insert_admin_user(conn, username="amy", role="viewer")
    with conn:
        conn.execute("UPDATE admin_users SET email = 'amy@example.com' WHERE username = 'amy'")
    insert_user_pick(conn, "amy", title="Dune", tmdb_id=42, poster_url=POSTER)
    now = datetime.now(UTC)

    picks = load_own_picks(conn, ["amy@example.com"], now, base_url=SITE, secret_key=secret_key)

    (pick,) = picks["amy@example.com"]
    assert (pick["poster_url"] or "").startswith(f"{SITE}/api/img/tmdb/w342/p.jpg?sig=")
