"""Tests for what an older TV fetches with a ticket: a film as one MPEG-TS, the test picture."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from mediaman.db import get_db
from mediaman.services.dlna.media import Media
from mediaman.services.media_meta import plex_stream
from mediaman.web.auth.session_store import destroy_session
from mediaman.web.routes import cast, older_stream
from mediaman.web.routes.watch import stream_api
from tests.helpers.factories import insert_settings
from tests.unit.web.test_cast import (  # noqa: F401
    PLEX_URL,
    TOKEN,
    FakeResponse,
    _cast,
    _ticket,
    clock,
    plex,
)

PLAYLIST = (
    "#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXTINF:8.0,\n00000.ts\n#EXTINF:8.0,\n00001.ts\n"
    "#EXTINF:4.5,\n00002.ts\n#EXT-X-ENDLIST\n"
)


@pytest.fixture(autouse=True)
def film(plex, monkeypatch):  # noqa: F811
    """Three segments, each of its own bytes; what fails, by name."""
    real = plex.request
    plex.playlist, plex.fails = PLAYLIST, {}

    def request(method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        if url.endswith("/base/index.m3u8"):
            return FakeResponse(200, plex.playlist.encode())
        if name.endswith(".ts"):
            plex.calls.append((name, {}))
            if plex.fails.get(name, 0):
                plex.fails[name] -= 1
                return FakeResponse(500)
            return FakeResponse(200, f"<{name}>".encode())
        return real(method, url, **kwargs)

    monkeypatch.setattr(plex, "request", request)
    monkeypatch.setattr(older_stream, "_WAITS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(older_stream, "_feeds", {})
    return plex


@pytest.fixture
def app(app_factory, conn, plex):  # noqa: F811
    insert_settings(
        conn,
        plex_url=PLEX_URL,
        plex_token=TOKEN,
        plex_libraries='["1"]',
        older_tvs_enabled="true",
    )
    return app_factory(cast.router, stream_api.router, older_stream.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


@pytest.fixture
def tv(app):
    return TestClient(app)


@pytest.fixture
def ticket(alice) -> str:
    return _ticket(_cast(alice)["url"])


def _from(ticket: str, place: int | str = 0) -> str:
    return f"/api/cast/{ticket}/from/{place}.ts"


def test_a_tv_reads_the_film_as_one_from_the_start_or_a_place(tv, ticket):
    whole = tv.get(_from(ticket))
    assert whole.status_code == 200
    assert whole.headers["content-type"] == "video/mpeg"
    assert whole.headers["cache-control"] == "no-store, no-transform"
    assert "set-cookie" not in whole.headers
    assert whole.content == b"<00000.ts><00001.ts><00002.ts>"
    assert tv.get(_from(ticket, 2)).content == b"<00002.ts>"


def test_the_film_is_converted_to_what_any_tv_plays(tv, ticket, film):
    asked = film.decision()
    profile = asked["X-Plex-Client-Profile-Extra"]
    assert "container=mpegts&videoCodec=h264&audioCodec=aac" in profile
    assert "name=video.level&value=41" in profile
    assert "name=audio.channels&value=2" in profile
    assert asked["videoResolution"] == "1920x1080"


def test_a_place_is_the_segment_that_holds_it():
    parts = [("0.ts", 0.0), ("1.ts", 8.0), ("2.ts", 16.0)]
    assert older_stream.place_of(parts, 15.9) == (1, 8.0)
    assert older_stream.place_of(parts, 16.0) == (2, 16.0)
    assert older_stream.place_of(parts, 9999) == (2, 16.0)


def test_no_byte_range_is_offered_and_one_asked_for_anyway_gets_the_whole(tv, ticket):
    resp = tv.get(_from(ticket), headers={"Range": "bytes=10-", "getcontentFeatures.dlna.org": "1"})
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "none"
    assert "content-range" not in resp.headers
    assert resp.content.startswith(b"<00000.ts>")
    features = resp.headers["contentfeatures.dlna.org"]
    assert "DLNA.ORG_OP=00;DLNA.ORG_CI=1;" in features
    assert features in older_stream.film_media("http://tv/").didl()


def test_a_jump_in_time_is_refused_and_one_to_the_start_is_not(tv, ticket):
    for asked, status in (("npt=120.0-", 406), ("npt=0.0-", 200), ("npt=0:00:00.000-", 200)):
        resp = tv.get(_from(ticket), headers={"TimeSeekRange.dlna.org": asked})
        assert resp.status_code == status, asked


def test_head_tells_the_kind_without_fetching_a_segment(tv, ticket, film):
    before = len(film.calls)
    resp = tv.head(_from(ticket), headers={"getcontentFeatures.dlna.org": "1"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mpeg"
    assert resp.headers["transfermode.dlna.org"] == "Streaming"
    assert "contentfeatures.dlna.org" in resp.headers
    assert "content-length" not in resp.headers
    assert len(film.calls) == before


@pytest.mark.parametrize("place", ["3", "x", "-1", "0000000"])
def test_a_place_past_the_end_or_none_at_all_is_refused(tv, ticket, place):
    assert tv.get(_from(ticket, place)).status_code == 404


def test_nothing_but_a_ticket_reads_the_film(tv, alice, ticket):
    assert tv.get(_from("A" * 43)).status_code == 404
    assert alice.get(_from("A" * 43)).status_code == 404


def test_a_film_ends_with_its_own_stream_and_no_other(tv, alice, ticket):
    other = _ticket(_cast(alice, "102")["url"])
    assert tv.get(_from(other)).status_code == 200
    assert _cast(alice, "102")
    assert tv.get(_from(other)).status_code == 404
    assert tv.get(_from(ticket)).status_code == 200


def test_a_film_ends_with_the_session_that_started_it(tv, alice, ticket, conn):
    destroy_session(conn, alice.cookies["session_token"])
    assert tv.get(_from(ticket)).status_code == 404


def test_the_ticket_is_asked_again_before_each_segment(tv, ticket, monkeypatch):
    real = older_stream._read_segment

    async def then_signs_out(request, stream_id, user, name):
        answer = await real(request, stream_id, user, name)
        with get_db() as here:
            here.execute("DELETE FROM admin_sessions")
        return answer

    monkeypatch.setattr(older_stream, "_read_segment", then_signs_out)
    assert tv.get(_from(ticket)).content == b"<00000.ts>"


def test_no_film_is_read_while_older_tvs_are_off(tv, ticket, conn):
    conn.execute("DELETE FROM settings WHERE key = 'older_tvs_enabled'")
    conn.commit()
    assert tv.get(_from(ticket)).status_code == 404
    assert tv.head(_from(ticket)).status_code == 404


def _stream() -> str:
    (stream_id,) = plex_stream.get_stream_manager()._streams
    return stream_id


def test_a_segment_plex_fails_once_costs_nothing(tv, ticket, film):
    film.fails = {"00001.ts": 1}
    assert tv.get(_from(ticket)).content == b"<00000.ts><00001.ts><00002.ts>"
    assert older_stream.sent(_stream()) == (True, False)


def test_a_film_that_broke_off_is_told_from_one_sent_whole(tv, ticket, film):
    film.fails = {"00001.ts": 99}
    assert tv.get(_from(ticket)).content == b"<00000.ts>"
    assert older_stream.sent(_stream()) == (False, True)
    film.fails = {}
    tv.get(_from(ticket))
    assert older_stream.sent(_stream()) == (True, False)


def test_a_stream_that_is_no_mpeg_ts_is_not_sent_as_one(tv, ticket, film):
    film.playlist = PLAYLIST.replace(".ts", ".m4s")
    assert tv.get(_from(ticket)).status_code == 502


def test_a_tv_holds_only_so_many_copies_open(tv, ticket):
    older_stream._feed(_stream()).open = older_stream._MAX_OPEN
    assert tv.get(_from(ticket)).status_code == 429


def test_the_ticket_is_kept_out_of_the_log(tv, ticket, film, caplog):
    film.fails = {"00001.ts": 99}
    with caplog.at_level(logging.DEBUG, logger="mediaman"):
        tv.get(_from(ticket))
    assert ticket not in caplog.text


# ── the test picture ──


def test_the_test_picture_is_a_small_jpeg_for_its_token_alone(tv):
    token, picture = older_stream.offer_picture()
    resp = tv.get(f"/api/cast/{token}/test.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content.startswith(b"\xff\xd8\xff")
    assert len(resp.content) < 64 * 1024
    assert picture.fetched.is_set()
    assert tv.get(f"/api/cast/{'A' * 43}/test.jpg").status_code == 404


def test_a_look_at_the_headers_is_no_fetch_of_the_picture(tv):
    token, picture = older_stream.offer_picture()
    resp = tv.head(f"/api/cast/{token}/test.jpg", headers={"getcontentFeatures.dlna.org": "1"})
    assert resp.headers["transfermode.dlna.org"] == "Interactive"
    assert resp.headers["contentfeatures.dlna.org"] == _picture().content_features
    assert not picture.fetched.is_set()


def _picture() -> Media:
    return older_stream.picture_media("http://tv/")


def test_a_withdrawn_or_old_picture_is_gone(tv, monkeypatch):
    token, _ = older_stream.offer_picture()
    older_stream.withdraw_picture(token)
    assert tv.get(f"/api/cast/{token}/test.jpg").status_code == 404
    monkeypatch.setattr(older_stream, "PICTURE_SECONDS", -1.0)
    token, _ = older_stream.offer_picture()
    assert tv.get(f"/api/cast/{token}/test.jpg").status_code == 404


def test_a_pictures_token_reads_no_stream(tv, ticket):
    token, _ = older_stream.offer_picture()
    assert tv.get(_from(token)).status_code == 404
    assert tv.get(f"/api/cast/{token}/index.m3u8").status_code == 404
