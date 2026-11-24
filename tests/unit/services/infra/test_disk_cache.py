"""The disk cache: fresh, stale-then-refreshed, miss, failures not stored, LRU eviction."""

from __future__ import annotations

import gc
import hashlib
import os
import threading
import weakref

import pytest

from mediaman.services.infra import SafeHTTPError, disk_cache


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


def test_a_failure_re_raised_during_the_backoff_keeps_its_errno_and_message():
    fetch = Fetcher(OSError(111, "Connection refused"), "late")
    store = disk_cache.meta()
    with pytest.raises(OSError):
        store.get_or_fetch("k", DAY, DAY, fetch)

    with pytest.raises(OSError) as again:
        store.get_or_fetch("k", DAY, DAY, fetch)

    assert again.value.errno == 111
    assert "Connection refused" in str(again.value)


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


class HeldPool:
    """An executor that queues work and never runs it."""

    def __init__(self):
        self.queued = []

    def submit(self, fn):
        self.queued.append(fn)


def test_stale_hits_while_a_refresh_waits_in_the_queue_submit_only_one(monkeypatch):
    fetch = Fetcher("old", "new")
    store = disk_cache.meta()
    store.get_or_fetch("k", 0, 7 * DAY, fetch)
    pool = HeldPool()
    monkeypatch.setattr(disk_cache, "sync", False)
    monkeypatch.setattr(disk_cache, "_executor", lambda: pool)

    for _ in range(3):
        assert store.get_or_fetch("k", 0, 7 * DAY, fetch) == "old"

    assert len(pool.queued) == 1
    pool.queued[0]()
    assert fetch.calls == 2
    assert store.get_or_fetch("k", DAY, 7 * DAY, fetch) == "new"


def test_a_waiter_is_answered_even_when_storing_the_value_fails(monkeypatch):
    store = disk_cache.meta()

    def broken_encode(value):
        raise TypeError("not storable")

    monkeypatch.setattr(store, "_encode", broken_encode)
    started = threading.Event()
    release = threading.Event()
    joined = threading.Event()
    claim = disk_cache._claim

    def noting_claim(flight):
        future, owner = claim(flight)
        if not owner:
            joined.set()
        return future, owner

    monkeypatch.setattr(disk_cache, "_claim", noting_claim)

    def slow():
        started.set()
        release.wait(5)
        return "v"

    owner_error = []

    def owner():
        try:
            store.get_or_fetch("k", DAY, DAY, slow)
        except TypeError as exc:
            owner_error.append(exc)

    waited = []
    first = threading.Thread(target=owner, daemon=True)
    first.start()
    started.wait(5)
    second = threading.Thread(
        target=lambda: waited.append(store.get_or_fetch("k", DAY, DAY, slow)), daemon=True
    )
    second.start()
    assert joined.wait(5)
    release.set()
    first.join(5)
    second.join(5)

    assert waited == ["v"]
    assert len(owner_error) == 1


def test_a_backed_off_failure_holds_no_frame_of_the_request_that_failed():
    class Marker:
        pass

    store = disk_cache.meta()
    refs = []

    def request():
        marker = Marker()
        refs.append(weakref.ref(marker))
        with pytest.raises(LookupError):
            store.get_or_fetch("k", DAY, DAY, Fetcher(LookupError("down")))

    request()
    gc.collect()
    assert refs[0]() is None

    with pytest.raises(LookupError) as first:
        store.get_or_fetch("k", DAY, DAY, Fetcher())
    with pytest.raises(LookupError) as second:
        store.get_or_fetch("k", DAY, DAY, Fetcher())
    assert first.value is not second.value
    assert str(second.value) == "down"


def test_a_backed_off_error_keeps_its_attributes():
    store = disk_cache.meta()
    error = SafeHTTPError(status_code=404, body_snippet="gone", url="https://x.invalid/a")
    with pytest.raises(SafeHTTPError):
        store.get_or_fetch("k", DAY, DAY, Fetcher(error))

    with pytest.raises(SafeHTTPError) as again:
        store.get_or_fetch("k", DAY, DAY, Fetcher())

    assert again.value.status_code == 404


def test_startup_removes_old_temp_files_and_keeps_fresh_ones(cache, tmp_path):
    folder = cache / "meta"
    old = folder / "abandoned.tmp"
    live = folder / "writing.tmp"
    old.write_bytes(b"half")
    live.write_bytes(b"half")
    os.utime(old, (1000, 1000))

    disk_cache.init(tmp_path)

    assert not old.exists()
    assert live.exists()


def _stored_bytes(folder) -> int:
    return sum(p.stat().st_size for p in folder.iterdir())


def test_no_write_leaves_the_store_over_its_cap_by_more_than_one_entry(cache, monkeypatch):
    store = disk_cache.img()
    monkeypatch.setattr(store, "max_bytes", 10_000)
    largest = 0
    for n in range(60):
        store.get_or_fetch(f"k{n}", DAY, DAY, Fetcher((b"x" * 1000, {})))
        largest = max(largest, (cache / "img" / _name(f"k{n}")).stat().st_size)
        assert _stored_bytes(cache / "img") <= 10_000 + largest


def test_the_cap_counts_what_was_stored_before_a_restart(cache, tmp_path, monkeypatch):
    store = disk_cache.img()
    for n in range(8):
        store.get_or_fetch(f"k{n}", DAY, DAY, Fetcher((b"x" * 1000, {})))
    monkeypatch.setattr(store, "_bytes", 0)  # a fresh process knows nothing yet
    monkeypatch.setattr(store, "max_bytes", 5000)

    disk_cache.init(tmp_path)
    store.get_or_fetch("after", DAY, DAY, Fetcher((b"x" * 100, {})))

    assert _stored_bytes(cache / "img") <= 5000


def test_each_waiter_on_a_failed_fetch_gets_its_own_copy_of_the_error(monkeypatch):
    claim = disk_cache._claim
    joined = threading.Semaphore(0)

    def noting_claim(flight):
        future, owner = claim(flight)
        if not owner:
            joined.release()
        return future, owner

    monkeypatch.setattr(disk_cache, "_claim", noting_claim)
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)
        raise LookupError("down")

    store = disk_cache.meta()
    errors: list[BaseException] = []

    def ask():
        try:
            store.get_or_fetch("k", DAY, DAY, slow)
        except LookupError as exc:
            errors.append(exc)

    first = threading.Thread(target=ask, daemon=True)
    first.start()
    started.wait(5)
    waiters = [threading.Thread(target=ask, daemon=True) for _ in range(2)]
    for waiter in waiters:
        waiter.start()
    assert joined.acquire(timeout=5) and joined.acquire(timeout=5)
    release.set()
    for thread in (first, *waiters):
        thread.join(5)

    assert len(errors) == 3
    assert len({id(e) for e in errors}) == 3
    assert {str(e) for e in errors} == {"down"}


def test_a_peek_at_a_miss_answers_none_and_fills_the_entry_for_the_next_one():
    store = disk_cache.meta()
    fetch = Fetcher("v")

    assert store.peek("k", DAY, DAY, fetch) is None
    assert store.peek("k", DAY, DAY, fetch) == "v"
    assert fetch.calls == 1


def test_a_peek_does_not_ask_again_for_a_key_that_just_failed():
    store = disk_cache.meta()
    fetch = Fetcher(LookupError("down"), "v")

    assert store.peek("k", DAY, DAY, fetch) is None
    assert store.peek("k", DAY, DAY, fetch) is None
    assert fetch.calls == 1
