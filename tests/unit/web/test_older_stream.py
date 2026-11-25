"""Tests for what an older TV fetches with a ticket: a film as one MPEG-TS, the test picture."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from mediaman.db import get_db
from mediaman.services.dlna.media import Media
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import StreamGone, StreamManager
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


def test_a_film_that_fails_in_any_way_broke_off(tv, ticket, monkeypatch):
    async def fails(*args):
        raise RuntimeError("no database")

    monkeypatch.setattr(older_stream, "_read_segment", fails)
    assert tv.get(_from(ticket)).content == b""
    assert older_stream.sent(_stream()) == (False, True)


def test_a_film_that_cannot_be_opened_again_broke_off(tv, ticket, film):
    tv.get(_from(ticket))
    older_stream.hand_over(_stream(), 0)
    film.playlist = "nothing"
    assert tv.get(_from(ticket)).status_code == 502
    assert older_stream.sent(_stream()) == (False, True)


def test_a_tv_that_hangs_up_broke_nothing_off(ticket, app):
    with TestClient(app).stream("GET", _from(ticket)) as resp:
        next(resp.iter_raw())
    assert older_stream.sent(_stream())[1] is False


def test_a_stream_that_is_no_mpeg_ts_is_not_sent_as_one(tv, ticket, film):
    film.playlist = PLAYLIST.replace(".ts", ".m4s")
    assert tv.get(_from(ticket)).status_code == 502


class Reader:
    """A TV that takes as much of a film as it is let, over ASGI itself."""

    def __init__(self, app, path: str) -> None:
        self.app, self.path = app, path
        self.sent = b""
        self.status = 0
        self.takes = asyncio.Event()
        self.hung_up = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def receive(self):
        if not self.status:
            self.status = -1
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            return
        self.sent += message["body"]
        if message.get("more_body"):
            await self.takes.wait()

    def open(self) -> Reader:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("192.168.1.20", 5),
            "server": ("192.168.1.10", 8282),
        }
        self.task = asyncio.create_task(self.app(scope, self.receive, self.send))
        return self

    async def until(self, true) -> None:
        for _ in range(500):
            if true():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("it never came")

    async def reads(self) -> Reader:
        await self.until(lambda: self.sent)
        return self

    async def is_over(self) -> None:
        await self.until(self.task.done)


async def _finishes(reader: Reader) -> None:
    reader.takes.set()
    await reader.is_over()


async def _hangs_up(reader: Reader) -> None:
    reader.hung_up.set()
    await reader.is_over()


def test_a_copy_opened_and_dropped_takes_nothing_from_the_one_read_to_its_end(app, ticket):
    async def run():
        first = await Reader(app, _from(ticket)).open().reads()
        second = await Reader(app, _from(ticket)).open().reads()
        await _hangs_up(second)
        await _finishes(first)

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (True, False)


def test_a_copy_that_is_refused_takes_nothing_from_the_one_read_to_its_end(app, ticket, film):
    async def run():
        first = await Reader(app, _from(ticket)).open().reads()
        film.playlist = "nothing"
        second = Reader(app, _from(ticket)).open()
        await second.is_over()
        assert second.status == 502
        assert older_stream.sent(_stream()) == (False, False)
        await _finishes(first)

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (True, False)


def test_a_copy_that_was_refused_makes_no_error_of_a_stop(app, ticket, film):
    async def run():
        first = await Reader(app, _from(ticket)).open().reads()
        film.playlist = "nothing"
        await Reader(app, _from(ticket)).open().is_over()
        await _hangs_up(first)

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (False, False)


def test_a_copy_that_failed_counts_once_no_other_is_open(app, ticket, film):
    async def run():
        first = await Reader(app, _from(ticket)).open().reads()
        second = await Reader(app, _from(ticket)).open().reads()
        film.fails = {"00001.ts": 99}
        await _finishes(second)
        assert older_stream.sent(_stream()) == (False, False)
        await _finishes(first)

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (False, True)


def test_a_copy_opened_after_one_that_failed_makes_up_for_it(app, ticket, film):
    async def run():
        film.fails = {"00001.ts": 99}
        await _finishes(await Reader(app, _from(ticket)).open().reads())
        film.fails = {}
        await _hangs_up(await Reader(app, _from(ticket)).open().reads())

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (False, False)


def test_a_film_handed_over_anew_has_not_been_sent_though_from_the_same_place(app, ticket, tv):
    tv.get(_from(ticket))
    assert older_stream.sent(_stream()) == (True, False)
    older_stream.hand_over(_stream(), 0)
    assert older_stream.sent(_stream()) == (False, False)

    async def run():
        earlier = await Reader(app, _from(ticket)).open().reads()
        older_stream.hand_over(_stream(), 0)
        await _finishes(earlier)

    asyncio.run(run())
    assert older_stream.sent(_stream()) == (False, False)
    tv.get(_from(ticket))
    assert older_stream.sent(_stream()) == (True, False)


def test_only_the_place_handed_over_last_counts(tv, ticket, film):
    older_stream.hand_over(_stream(), 1)
    tv.get(_from(ticket, 0))
    assert older_stream.sent(_stream()) == (False, False)
    film.fails = {"00000.ts": 99}
    tv.get(_from(ticket, 0))
    assert older_stream.sent(_stream()) == (False, False)
    tv.get(_from(ticket, 1))
    assert older_stream.sent(_stream()) == (True, False)


def test_a_place_handed_over_shares_the_copies_there_may_be(app, ticket):
    async def run():
        first = [await Reader(app, _from(ticket)).open().reads() for _ in range(3)]
        older_stream.hand_over(_stream(), 1)
        await Reader(app, _from(ticket, 1)).open().reads()
        await first[0].is_over()
        assert len(older_stream._feed(_stream()).readers) == 3

    asyncio.run(run())


@pytest.fixture
def ending():
    """What says that mediaman is told to end."""
    yield older_stream.ending
    older_stream.ending.clear()


def test_a_film_is_let_go_of_once_mediaman_is_told_to_end(app, ticket, ending, monkeypatch):
    monkeypatch.setattr(older_stream, "_LIVES_SECONDS", 0.02)

    async def run():
        reader = await Reader(app, _from(ticket)).open().reads()
        ending.set()
        await reader.is_over()
        late = Reader(app, _from(ticket)).open()
        await late.is_over()
        assert late.status == 503

    asyncio.run(run())


@pytest.mark.parametrize("how", ["told", "hung up"])
def test_a_film_that_ends_hangs_up_on_the_segment_plex_owes_it(
    app, ticket, ending, monkeypatch, how
):
    monkeypatch.setattr(older_stream, "_LIVES_SECONDS", 0.02)
    asked, let_go = threading.Event(), threading.Event()

    def segment(self, read, name):
        asked.set()
        for _ in range(200):
            if read.unwanted:
                let_go.set()
                break
            time.sleep(0.01)
        raise StreamGone("gone")

    monkeypatch.setattr(StreamManager, "segment", segment)

    async def run():
        reader = Reader(app, _from(ticket)).open()
        await reader.until(asked.is_set)
        if how == "told":
            ending.set()
        else:
            reader.hung_up.set()
        await reader.until(let_go.is_set)
        await reader.is_over()

    asyncio.run(run())


def test_a_fourth_copy_ends_the_oldest_and_not_itself(app, ticket):
    async def run():
        first = [await Reader(app, _from(ticket)).open().reads() for _ in range(3)]
        fourth = await Reader(app, _from(ticket)).open().reads()
        await first[0].is_over()
        assert [r.task.done() for r in (*first[1:], fourth)] == [False] * 3
        assert len(older_stream._feeds[_stream()].readers) == 3
        assert older_stream.sent(_stream()) == (False, False)
        for reader in (*first[1:], fourth):
            reader.hung_up.set()
            await reader.is_over()
        assert older_stream._feeds[_stream()].readers == []

    asyncio.run(run())


def test_of_twenty_copies_at_once_three_stay_open(app, ticket, monkeypatch):
    finding: list[int] = []
    asked = []
    real = older_stream.segments

    def segments(*args):
        finding.append(1)
        asked.append(len(finding))
        time.sleep(0.02)
        finding.pop()
        return real(*args)

    monkeypatch.setattr(older_stream, "segments", segments)

    async def run():
        readers = [Reader(app, _from(ticket)).open() for _ in range(20)]
        feed = older_stream._feed(_stream())
        most = 0
        for _ in range(100):
            most = max(most, len(feed.readers))
            await asyncio.sleep(0.01)
        going = [r for r in readers if not r.task.done()]
        assert (most, len(going), max(asked)) == (3, 3, 3)
        assert {r.status for r in readers} <= {200, 429}
        for reader in readers:
            reader.hung_up.set()
            await reader.is_over()

    asyncio.run(run())


def test_a_tv_that_hangs_up_lets_go_of_its_segment_at_once(app, ticket, monkeypatch):
    closed = []
    real = older_stream._film

    async def film(*args):
        try:
            async for chunk in real(*args):
                yield chunk
        finally:
            closed.append(True)

    monkeypatch.setattr(older_stream, "_film", film)

    async def run():
        reader = await Reader(app, _from(ticket)).open().reads()
        reader.hung_up.set()
        await reader.is_over()
        assert closed == [True]

    asyncio.run(run())


def test_a_tv_that_stopped_reading_lets_go_once_its_stream_ends(app, ticket, monkeypatch):
    monkeypatch.setattr(older_stream, "_LIVES_SECONDS", 0.02)

    async def run():
        reader = await Reader(app, _from(ticket)).open().reads()
        await asyncio.sleep(0.1)
        assert not reader.task.done()
        plex_stream.get_stream_manager().end(_stream(), "alice")
        await reader.is_over()
        assert all(not feed.readers for feed in older_stream._feeds.values())

    asyncio.run(run())


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
