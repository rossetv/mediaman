"""TMDB images for Watch: served from the disk cache, fetched only from image.tmdb.org, and
only known sizes and plain file names."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import image_api
from tests.helpers.factories import insert_admin_user

PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_root", None)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "_inflight", {})
    monkeypatch.setattr(disk_cache, "sync", True)
    disk_cache.init(tmp_path)


@pytest.fixture
def client(app_factory, conn):
    insert_admin_user(conn, username="aoife", role="viewer")
    test_client = TestClient(app_factory(image_api.router, conn=conn))
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


@pytest.fixture
def upstream(monkeypatch):
    calls: list[tuple[str, str]] = []
    answers: list[object] = []

    def fake(size, name):
        calls.append((size, name))
        answer = answers.pop(0) if answers else (PNG, "image/png")
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(image_api, "_fetch_tmdb_image", fake)
    return SimpleNamespace(calls=calls, answers=answers)


def test_a_viewer_gets_the_image_with_a_long_private_cache(client, upstream):
    resp = client.get("/api/watch/img/tmdb/w342/abc_DEF-1.png")

    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=2592000, immutable"
    assert upstream.calls == [("w342", "abc_DEF-1.png")]


def test_a_second_request_is_served_from_the_cache(client, upstream):
    client.get("/api/watch/img/tmdb/w342/a.jpg")

    resp = client.get("/api/watch/img/tmdb/w342/a.jpg")

    assert resp.status_code == 200
    assert len(upstream.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/watch/img/tmdb/w9999/a.jpg",
        "/api/watch/img/tmdb/original/a.jpg",
        "/api/watch/img/tmdb/w342/a.svg",
        "/api/watch/img/tmdb/w342/a.b.jpg",
        "/api/watch/img/tmdb/w342/..%2Fa.jpg",
        "/api/watch/img/tmdb/w342/" + "a" * 65 + ".jpg",
    ],
)
def test_a_bad_size_or_name_is_404_without_a_fetch(client, upstream, path):
    assert client.get(path).status_code == 404
    assert upstream.calls == []


def test_an_upstream_failure_is_503_and_not_cached(client, upstream, monkeypatch):
    monkeypatch.setattr(disk_cache, "BACKOFF_S", 0.0)
    upstream.answers.append(SafeHTTPError(502, "bad gateway", "https://image.tmdb.org/t/p"))

    assert client.get("/api/watch/img/tmdb/w342/a.jpg").status_code == 503
    assert client.get("/api/watch/img/tmdb/w342/a.jpg").status_code == 200


def test_an_image_tmdb_does_not_have_is_404(client, upstream):
    upstream.answers.append(SafeHTTPError(404, "", "https://image.tmdb.org/t/p"))

    assert client.get("/api/watch/img/tmdb/w342/a.jpg").status_code == 404


def test_cache_hits_are_not_rate_limited_but_fetches_are(client, upstream, monkeypatch):
    monkeypatch.setattr(image_api, "_IMG_LIMITER", ActionRateLimiter(1, 60))

    hits = [client.get("/api/watch/img/tmdb/w342/a.jpg").status_code for _ in range(3)]
    other = client.get("/api/watch/img/tmdb/w342/b.jpg").status_code

    assert hits == [200, 200, 200]
    assert other == 429
    assert len(upstream.calls) == 1


def test_an_image_host_that_hangs_holds_no_worker_thread_and_nobody_for_long(
    client, upstream, monkeypatch
):
    client.get("/api/watch/img/tmdb/w342/held.jpg")
    release, threads = threading.Event(), set()

    def hang(size, name):
        threads.add(threading.current_thread().name)
        release.wait(10)
        return PNG, "image/png"

    monkeypatch.setattr(image_api, "_fetch_tmdb_image", hang)
    monkeypatch.setattr(image_api, "_FETCH_WAIT_S", 0.05)
    try:
        codes = [client.get(f"/api/watch/img/tmdb/w342/{n}.jpg").status_code for n in range(10)]
        held = client.get("/api/watch/img/tmdb/w342/held.jpg").status_code
    finally:
        release.set()

    assert codes == [503] * 10
    assert held == 200
    assert all(name.startswith("tmdb-image") for name in threads)


def test_needs_a_session(client, upstream):
    client.cookies.clear()

    assert client.get("/api/watch/img/tmdb/w342/a.jpg").status_code == 401


class TestFetch:
    @pytest.fixture
    def http(self, monkeypatch):
        seen: list[str] = []
        reply = SimpleNamespace(headers={"Content-Type": "image/jpeg"}, content=b"\xff\xd8jpg")

        def get(url, **_kwargs):
            seen.append(url)
            return reply

        monkeypatch.setattr(image_api, "_TMDB_HTTP", SimpleNamespace(get=get))
        return SimpleNamespace(seen=seen, reply=reply)

    def test_asks_image_tmdb_org_for_that_size_and_name(self, http):
        assert image_api._fetch_tmdb_image("w500", "x.jpg") == (b"\xff\xd8jpg", "image/jpeg")
        assert http.seen == ["https://image.tmdb.org/t/p/w500/x.jpg"]

    def test_a_type_outside_the_image_allowlist_is_refused(self, http):
        http.reply.headers["Content-Type"] = "image/svg+xml"

        with pytest.raises(SafeHTTPError):
            image_api._fetch_tmdb_image("w500", "x.jpg")
