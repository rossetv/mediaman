"""TMDB pictures by signed address, for a page or mail opened with no session: one picture per
signature, from the same cache as Watch's, and a uniform 401 for anything else."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mediaman.core.scrub_filter import AccessLogFilter
from mediaman.crypto import generate_poster_token, sign_tmdb_image_url
from mediaman.services.infra import disk_cache
from mediaman.services.rate_limit import RateLimiter
from mediaman.web.routes.watch import image_api, signed_image_api

PNG = b"\x89PNG\r\n\x1a\nimage"
DAY = 86400


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_root", None)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "_inflight", {})
    monkeypatch.setattr(disk_cache, "sync", True)
    disk_cache.init(tmp_path)


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    monkeypatch.setattr(signed_image_api, "_PUBLIC_LIMITER", RateLimiter(60, 60))


@pytest.fixture
def upstream(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake(size, name):
        calls.append((size, name))
        return PNG, "image/png"

    monkeypatch.setattr(image_api, "_fetch_tmdb_image", fake)
    return SimpleNamespace(calls=calls)


@pytest.fixture
def app(app_factory, conn):
    return app_factory(signed_image_api.router, image_api.router, conn=conn)


@pytest.fixture
def client(app):
    return TestClient(app, client=("203.0.113.5", 50000))


def _signed(secret_key, image="/abc.jpg", size="w500", days=180):
    url = sign_tmdb_image_url(image, size, secret_key, expires_at=int(time.time()) + days * DAY)
    assert url is not None
    return url


def test_a_signed_address_gives_the_picture_with_no_session(client, upstream, secret_key):
    resp = client.get(_signed(secret_key))

    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert upstream.calls == [("w500", "abc.jpg")]


def test_the_signature_is_no_part_of_the_cache_key(client, upstream, secret_key):
    assert client.get(_signed(secret_key, days=10)).status_code == 200
    assert client.get(_signed(secret_key, days=20)).status_code == 200

    assert upstream.calls == [("w500", "abc.jpg")]


def test_a_picture_watch_fetched_is_served_from_its_cache(client, upstream, secret_key):
    disk_cache.img().get_or_fetch(
        "img:tmdb:w500:abc.jpg", 60, 60, lambda: (PNG, {"mime": "image/png"})
    )

    assert client.get(_signed(secret_key)).content == PNG
    assert upstream.calls == []


def _refusals(secret_key):
    good = _signed(secret_key)
    path, _, sig = good.partition("?sig=")
    head, _, tail = sig.partition(".")
    return {
        "missing": path,
        "empty": f"{path}?sig=",
        "tampered": f"{path}?sig={head}.{'A' * len(tail)}",
        "another size": good.replace("/w500/", "/w342/"),
        "another name": good.replace("/abc.jpg", "/abd.jpg"),
        "expired": _signed(secret_key, days=-2),
        "another key": _signed("x" * 64),
        "a poster token": f"{path}?sig={generate_poster_token(rating_key='1', secret_key=secret_key)}",
        "too long": f"{path}?sig={'a' * 5000}",
        "a size not served": _signed(secret_key, size="original"),
        "a name not served": f"/api/img/tmdb/w500/a%2E%2E.jpg?sig={sig}",
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "tampered",
        "another size",
        "another name",
        "expired",
        "another key",
        "a poster token",
        "too long",
        "a size not served",
        "a name not served",
    ],
)
def test_anything_but_a_good_signature_is_the_same_401_and_fetches_nothing(
    client, upstream, secret_key, case
):
    resp = client.get(_refusals(secret_key)[case])

    assert (resp.status_code, resp.content) == (401, b"")
    assert upstream.calls == []


def test_only_tmdb_pictures_can_be_signed():
    for image in (
        "https://elsewhere.example/t/p/w500/abc.jpg",
        "https://image.tmdb.org.evil.example/t/p/w500/abc.jpg",
        "http://image.tmdb.org/t/p/w500/abc.jpg",
        "/../abc.jpg",
        "/abc.svg",
        None,
    ):
        assert sign_tmdb_image_url(image, "w500", "k" * 64, expires_at=0) is None


def test_a_tmdb_address_is_signed_at_the_size_asked_for(secret_key):
    url = _signed(secret_key, image="https://image.tmdb.org/t/p/w300/abc.jpg")

    assert url.startswith("/api/img/tmdb/w500/abc.jpg?sig=")


def test_every_address_of_a_picture_made_on_a_day_is_the_same(secret_key):
    start = 1_800_000_000 - 1_800_000_000 % DAY
    urls = {
        sign_tmdb_image_url("/abc.jpg", "w500", secret_key, expires_at=start + offset)
        for offset in (1, 3600, DAY)
    }

    assert len(urls) == 1


def test_a_page_s_pictures_asked_again_and_again_from_one_network_are_never_refused(
    app, upstream, secret_key
):
    page = [_signed(secret_key, image=f"/face-{n}.jpg", size="w185") for n in range(12)]
    reader = TestClient(app, client=("203.0.113.5", 1))
    neighbour = TestClient(app, client=("203.0.113.77", 1))

    answers = {reader.get(url).status_code for _ in range(5) for url in page}
    answers |= {neighbour.get(url).status_code for _ in range(5) for url in page}

    assert answers == {200}


def test_a_network_that_is_refused_too_often_is_turned_away_before_the_signature_is_read(
    app, upstream, secret_key, monkeypatch
):
    monkeypatch.setattr(signed_image_api, "_PUBLIC_LIMITER", RateLimiter(2, 60))
    url = _signed(secret_key)
    near = TestClient(app, client=("203.0.113.5", 1))
    neighbour = TestClient(app, client=("203.0.113.77", 1))
    elsewhere = TestClient(app, client=("198.51.100.9", 1))

    assert [near.get(url).status_code for _ in range(3)] == [200, 200, 200]
    assert [near.get(url.partition("?")[0]).status_code for _ in range(2)] == [401, 401]
    assert neighbour.get(url).status_code == 429
    assert elsewhere.get(url).status_code == 200


def test_the_signature_is_kept_out_of_the_access_log(secret_key):
    url = _signed(secret_key)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, "%s", ("1.2.3.4", "GET", url, "1.1", 200), None
    )

    AccessLogFilter().filter(record)

    assert isinstance(record.args, tuple)
    assert record.args[2] == "/api/img/tmdb/w500/abc.jpg?sig=***"


@pytest.mark.parametrize("who", ["viewer", "admin", "nobody"])
def test_through_the_whole_app_anyone_with_the_signed_address_gets_the_picture(
    conn, upstream, viewer_client, authed_client, secret_key, who
):
    from mediaman.config import Config
    from mediaman.db import set_connection
    from mediaman.main import create_app

    set_connection(conn)
    app = create_app()
    app.state.config = Config(secret_key=secret_key)
    if who == "viewer":
        client = viewer_client(app, conn)
    elif who == "admin":
        client = authed_client(app, conn)
    else:
        client = TestClient(app)
    url = _signed(secret_key)

    assert client.get(url).content == PNG
    assert client.get(url.partition("?")[0]).status_code == 401
