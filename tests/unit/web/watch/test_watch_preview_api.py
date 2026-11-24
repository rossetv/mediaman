"""Frame previews: the owner's live stream only, from frames Plex already made, limited."""

from __future__ import annotations

import threading

import pytest

from mediaman.services.infra import disk_cache
from mediaman.services.media_meta import plex_stream
from mediaman.services.media_meta.plex_stream import StreamManager, _Stream
from mediaman.services.media_meta.plex_watch import PlexItemNotFound, PlexUnavailable, PlexWatch
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.routes.watch import preview_api

TOKEN = "tok-Zq81-only-in-tests"
PLEX_URL = "http://plex-lan.example.test:32400"
JPEG = b"\xff\xd8\xff\xe0frame"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_root", None)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "_inflight", {})
    disk_cache.init(tmp_path)


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(preview_api, "_LIMITER", ActionRateLimiter(120, 60))
    monkeypatch.setattr(preview_api, "_FETCH_SLOTS", threading.BoundedSemaphore(4))


class FakeFrames:
    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []
        self.error: Exception | None = None

    def fetch(self, part_id: str, offset_ms: int) -> bytes:
        self.asked.append((part_id, offset_ms))
        if self.error is not None:
            raise self.error
        return JPEG


@pytest.fixture
def plex(monkeypatch) -> FakeFrames:
    fake = FakeFrames()
    monkeypatch.setattr(
        PlexWatch, "fetch_preview", lambda self, part_id, ms: fake.fetch(part_id, ms)
    )
    return fake


def _stream(preview: dict | None) -> _Stream:
    return _Stream("alice", "101", "best", "sid", "/x", 1000.0, 1000.0, {"preview": preview})


@pytest.fixture
def manager(monkeypatch) -> StreamManager:
    manager = StreamManager(lambda: (PLEX_URL, TOKEN), clock=lambda: 1100.0)
    manager._streams["s1"] = _stream({"part_id": "31", "part_size": "99", "count": 10})
    manager._streams["s2"] = _stream(None)
    monkeypatch.setattr(plex_stream, "_manager", manager)
    return manager


@pytest.fixture
def app(app_factory, conn, plex, manager):
    conn.executemany(
        "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, 0, 'now')",
        [("plex_url", PLEX_URL), ("plex_token", TOKEN)],
    )
    conn.commit()
    return app_factory(preview_api.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


def _get(client, n: object, stream_id: str = "s1"):
    return client.get(f"/api/watch/stream/{stream_id}/preview/{n}")


def test_the_owner_gets_the_frame_of_that_moment_privately_cached(alice, plex):
    resp = _get(alice, 3)

    assert (resp.status_code, resp.content) == (200, JPEG)
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert plex.asked == [("31", 15000)]


def test_a_frame_is_fetched_once_until_the_file_changes(alice, plex, manager):
    _get(alice, 3)
    _get(alice, 3)
    assert len(plex.asked) == 1

    manager._streams["s1"].info["preview"]["part_size"] = "100"
    _get(alice, 3)
    assert len(plex.asked) == 2


def test_looking_at_previews_does_not_count_as_using_the_stream(alice, manager):
    assert _get(alice, 3).status_code == 200
    assert manager._streams["s1"].last_access == 1000.0


def test_someone_elses_stream_is_gone_exactly_like_an_unknown_one(
    app, conn, authed_client, alice, plex
):
    _get(alice, 3)
    bob = authed_client(app, conn, username="bob")

    theirs, unknown = _get(bob, 3), _get(bob, 3, "nope")

    assert theirs.status_code == unknown.status_code == 410
    assert theirs.content == unknown.content
    assert len(plex.asked) == 1


def test_needs_a_session(alice, plex):
    alice.cookies.clear()

    assert _get(alice, 3).status_code == 401
    assert plex.asked == []


@pytest.mark.parametrize(
    "n",
    ["10", "-1", "1.5", "x", "9" * 5000, "%C2%B2", "%D9%A3"],
    ids=["past the end", "negative", "fraction", "text", "huge", "superscript", "arabic digit"],
)
def test_a_frame_outside_the_title_is_404_before_plex(alice, plex, n):
    assert _get(alice, n).status_code == 404
    assert plex.asked == []


def test_a_title_without_previews_is_404(alice, plex):
    assert _get(alice, 0, "s2").status_code == 404
    assert plex.asked == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (PlexItemNotFound("no such frame"), 404, "not_found"),
        (PlexUnavailable("bad answer"), 502, "unreachable"),
    ],
)
def test_a_plex_failure_is_said_as_a_code_and_not_cached(
    alice, plex, monkeypatch, error, status, code
):
    monkeypatch.setattr(disk_cache, "BACKOFF_S", 0.0)
    plex.error = error
    resp = _get(alice, 3)
    assert (resp.status_code, resp.json()["error"]) == (status, code)

    plex.error = None
    assert _get(alice, 3).status_code == 200


def test_new_frames_are_limited_per_person_and_cached_ones_are_not(alice, monkeypatch):
    monkeypatch.setattr(preview_api, "_LIMITER", ActionRateLimiter(1, 60))

    assert _get(alice, 1).status_code == 200
    refused = _get(alice, 2)
    assert (refused.status_code, refused.json()["error"]) == (429, "rate_limited")
    assert _get(alice, 1).status_code == 200


def test_fetches_past_the_slots_are_refused_and_slots_are_given_back(alice, plex, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(preview_api, "_FETCH_SLOTS", slots)
    plex.error = PlexUnavailable("down")
    assert _get(alice, 1).status_code == 502
    plex.error = None
    assert _get(alice, 2).status_code == 200

    slots.acquire()
    assert _get(alice, 3).status_code == 429
    slots.release()
    assert _get(alice, 3).status_code == 200


def test_a_busy_moment_does_not_use_up_the_persons_limit(alice, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(preview_api, "_FETCH_SLOTS", slots)
    monkeypatch.setattr(preview_api, "_LIMITER", ActionRateLimiter(1, 60))
    slots.acquire()
    assert _get(alice, 1).status_code == 429
    slots.release()

    assert _get(alice, 1).status_code == 200
