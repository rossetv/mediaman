"""The disk cache: fresh, stale-then-refreshed, miss, failures not stored, LRU eviction."""

from __future__ import annotations

import hashlib
import os
import threading

import pytest

from mediaman.services.infra import disk_cache


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_root", None)
    monkeypatch.setattr(disk_cache, "_failed", {})
    monkeypatch.setattr(disk_cache, "_inflight", {})
    monkeypatch.setattr(disk_cache, "sync", True)
    disk_cache.init(tmp_path)
    return tmp_path / "watch_cache"


class Fetcher:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


DAY = 86400.0


def test_init_creates_both_store_folders(cache):
    assert (cache / "meta").is_dir()
    assert (cache / "img").is_dir()


def test_a_miss_fetches_and_a_fresh_entry_is_served_without_fetching():
    fetch = Fetcher({"title": "A"}, {"title": "B"})
    store = disk_cache.meta()

    first = store.get_or_fetch("tmdb:details:movie:1", DAY, 7 * DAY, fetch)
    second = store.get_or_fetch("tmdb:details:movie:1", DAY, 7 * DAY, fetch)

    assert first == second == {"title": "A"}
    assert fetch.calls == 1


def test_the_file_name_is_the_key_hash_and_holds_no_key_text(cache):
    disk_cache.meta().get_or_fetch("tmdb:search:../etc:1", DAY, DAY, Fetcher([1]))

    names = [p.name for p in (cache / "meta").iterdir()]
    assert len(names) == 1
    assert len(names[0]) == 64
    assert all(c in "0123456789abcdef" for c in names[0])


def test_a_stale_entry_is_served_then_refreshed():
    fetch = Fetcher("old", "new")
    store = disk_cache.meta()
    store.get_or_fetch("k", 0, 7 * DAY, fetch)

    served = store.get_or_fetch("k", 0, 7 * DAY, fetch)
    after = store.get_or_fetch("k", DAY, 7 * DAY, fetch)

    assert served == "old"
    assert after == "new"
    assert fetch.calls == 2


def test_an_entry_past_its_stale_limit_is_fetched_inline():
    fetch = Fetcher("old", "new")
    store = disk_cache.meta()
    store.get_or_fetch("k", 0, 7 * DAY, fetch)

    assert store.get_or_fetch("k", 0, 0, fetch) == "new"


def test_images_keep_their_bytes_and_info():
    fetch = Fetcher((b"\x89PNG\nbody", {"mime": "image/png"}))
    store = disk_cache.img()
    store.get_or_fetch("img:tmdb:w342:a.png", DAY, DAY, fetch)

    assert store.get_or_fetch("img:tmdb:w342:a.png", DAY, DAY, fetch) == (
        b"\x89PNG\nbody",
        {"mime": "image/png"},
    )
    assert fetch.calls == 1


def test_a_failure_is_not_stored_and_is_not_retried_within_the_backoff(cache):
    fetch = Fetcher(LookupError("down"), "late")
    store = disk_cache.meta()

    for _ in range(2):
        with pytest.raises(LookupError):
            store.get_or_fetch("k", DAY, DAY, fetch)

    assert fetch.calls == 1
    assert list((cache / "meta").iterdir()) == []


def test_after_the_backoff_a_failed_key_is_fetched_again(monkeypatch):
    monkeypatch.setattr(disk_cache, "BACKOFF_S", 0.0)
    fetch = Fetcher(LookupError("down"), "back")
    store = disk_cache.meta()
    with pytest.raises(LookupError):
        store.get_or_fetch("k", DAY, DAY, fetch)

    assert store.get_or_fetch("k", DAY, DAY, fetch) == "back"


def test_a_failed_refresh_keeps_serving_the_stale_value():
    fetch = Fetcher("old", LookupError("down"))
    store = disk_cache.meta()
    store.get_or_fetch("k", 0, 7 * DAY, fetch)

    assert store.get_or_fetch("k", 0, 7 * DAY, fetch) == "old"
    assert store.get_or_fetch("k", 0, 7 * DAY, fetch) == "old"
    assert fetch.calls == 2


def test_a_refused_miss_does_not_fetch_and_a_hit_is_not_asked():
    fetch = Fetcher("v")
    store = disk_cache.meta()
    with pytest.raises(disk_cache.Refused):
        store.get_or_fetch("k", DAY, DAY, fetch, allow_fetch=lambda: False)
    assert fetch.calls == 0

    store.get_or_fetch("k", DAY, DAY, fetch)

    def never():
        raise AssertionError("a hit asked for permission")

    assert store.get_or_fetch("k", DAY, DAY, fetch, allow_fetch=never) == "v"


def test_concurrent_misses_share_one_fetch():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return "v"

    store = disk_cache.meta()
    results = []
    first = threading.Thread(target=lambda: results.append(store.get_or_fetch("k", DAY, DAY, slow)))
    first.start()
    started.wait(5)
    second = threading.Thread(
        target=lambda: results.append(store.get_or_fetch("k", DAY, DAY, slow))
    )
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert results == ["v", "v"]
    assert len(calls) == 1


def test_an_unreadable_entry_is_refetched(cache):
    store = disk_cache.meta()
    store.get_or_fetch("k", DAY, DAY, Fetcher("v"))
    (entry,) = (cache / "meta").iterdir()
    entry.write_bytes(b"not a header")

    assert store.get_or_fetch("k", DAY, DAY, Fetcher("again")) == "again"


def test_past_the_cap_the_least_recently_used_entries_are_swept(cache, monkeypatch):
    store = disk_cache.img()
    monkeypatch.setattr(disk_cache, "_SWEEP_EVERY", 1)
    monkeypatch.setattr(store, "max_bytes", 3500)
    for n, key in enumerate(["a", "b", "c"]):
        store.get_or_fetch(key, DAY, DAY, Fetcher((b"x" * 1000, {})))
        path = cache / "img" / _name(key)
        os.utime(path, (1000 + n, 1000 + n))
    # A hit makes "a" the most recently used, so "b" is now the oldest.
    store.get_or_fetch("a", DAY, DAY, Fetcher())

    store.get_or_fetch("d", DAY, DAY, Fetcher((b"x" * 1000, {})))

    left = {p.name for p in (cache / "img").iterdir()}
    assert left == {_name("a"), _name("c"), _name("d")}


def test_without_a_data_dir_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(disk_cache, "_root", None)
    fetch = Fetcher("a", "b")
    store = disk_cache.meta()

    assert store.get_or_fetch("k", DAY, DAY, fetch) == "a"
    assert store.get_or_fetch("k", DAY, DAY, fetch) == "b"


def _name(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
