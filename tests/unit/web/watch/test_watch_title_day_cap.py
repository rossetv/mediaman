# ruff: noqa: F811
"""The title route's 429 says which limit it is, and for the day's cap, when lookups return."""

from __future__ import annotations

from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _discover
from mediaman.web.routes.watch import catalog_api
from tests.unit.web.watch.test_watch_catalog_api import (  # noqa: F401
    DictCache,
    _fresh_memo,
    arr,
    library,
    tmdb,
    viewer,
)


class TestTitleDayCap:
    def test_the_days_cap_says_so_and_when_lookups_come_back(self, viewer, tmdb, arr, monkeypatch):
        monkeypatch.setattr(_discover, "_cache", lambda: DictCache())
        monkeypatch.setattr(catalog_api, "_TITLE_DAY_LIMITER", ActionRateLimiter(1, 3600))

        viewer.get("/api/watch/title/tmdb-movie-12")
        resp = viewer.get("/api/watch/title/tmdb-movie-11")

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "title_day_limit"
        assert 3590 <= body["retry_after"] <= 3600
        assert resp.headers["Retry-After"] == str(body["retry_after"])

    def test_the_query_limit_keeps_the_plain_answer(self, viewer, tmdb, arr, monkeypatch):
        monkeypatch.setattr(_discover, "_cache", lambda: DictCache())
        monkeypatch.setattr(catalog_api, "_QUERY_LIMITER", ActionRateLimiter(0, 60))

        resp = viewer.get("/api/watch/title/tmdb-movie-11")

        assert resp.status_code == 429
        assert resp.json()["error"] == "too_many_requests"
        assert "Retry-After" not in resp.headers
