"""On-disk cache for slow remote lookups and images, kept across restarts.

Two stores live under ``<data_dir>/watch_cache``: ``meta`` holds JSON values (TMDB
lookups) and ``img`` holds image bytes with a small info dict (mime type and the like).
Each entry is one file named by the SHA-256 of its key, so no caller text reaches the
filesystem: a JSON header line (fetch time and info) followed by the payload, written to
a temp file and moved into place so a reader never sees half an entry.

``Store.get_or_fetch`` answers at once while an entry is fresh. Once stale (but within
``stale_s``) it still answers with the old value and refreshes it once in the background,
on a pool of two threads. On a miss it fetches inline, one fetch per key at a time; a
second caller waits for the first. A failed fetch is never stored: its exception is
re-raised for that key for ``BACKOFF_S`` seconds instead of asking upstream again.

The ``fetch`` callable may run on a pool thread, so it must not hold the request's
SQLite connection: build any client from the connection first, then close over the
client.

Until ``init`` names the data dir (at startup) nothing is stored and every call fetches.
Set ``sync = True`` (tests) to run the background refresh inline.

Each store is capped; every ``_SWEEP_EVERY`` writes a non-blocking sweep deletes the
least recently used files (by mtime, which a hit bumps) down to 90% of the cap, and any
temp file a crashed write left behind.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Image values: the bytes plus a small info dict (``{"mime": ...}`` and whatever the
#: caller needs to check again on a hit).
Image = tuple[bytes, dict[str, str]]

BACKOFF_S = 60.0
_BACKOFF_MAX_KEYS = 1024
_SWEEP_EVERY = 50
_TMP_MAX_AGE_S = 600.0
_META_MAX_BYTES = 64 * 1024 * 1024
_IMG_MAX_BYTES = 1024 * 1024 * 1024

#: Run background refreshes inline (tests).
sync = False

_root: Path | None = None

_guard = threading.Lock()
_inflight: dict[tuple[str, str], Future[Any]] = {}
_failed: dict[tuple[str, str], tuple[float, BaseException]] = {}
_pool: ThreadPoolExecutor | None = None


class Refused(Exception):
    """A miss was not fetched because ``allow_fetch`` said no (the caller's rate limit)."""


class Store[T]:
    def __init__(
        self,
        name: str,
        max_bytes: int,
        encode: Callable[[T], tuple[bytes, dict[str, Any]]],
        decode: Callable[[bytes, dict[str, Any]], T],
    ) -> None:
        self.name = name
        self.max_bytes = max_bytes
        self._encode = encode
        self._decode = decode
        self._writes = 0
        self._sweep_lock = threading.Lock()

    def get_or_fetch(
        self,
        key: str,
        fresh_s: float,
        stale_s: float,
        fetch: Callable[[], T],
        *,
        allow_fetch: Callable[[], bool] | None = None,
    ) -> T:
        """The value for *key*: cached while younger than *fresh_s*, cached and refreshed in
        the background while younger than *stale_s*, else fetched now.

        *allow_fetch* is asked only before an inline fetch on a miss; when it answers False
        this raises ``Refused``. Raises whatever *fetch* raised when there is nothing cached.
        """
        directory = self._dir()
        if directory is None:
            return fetch()
        path = directory / hashlib.sha256(key.encode()).hexdigest()
        cached = self._read(path)
        if cached is not None:
            age, value = cached
            if age < stale_s:
                with contextlib.suppress(OSError):
                    os.utime(path)
                if age >= fresh_s and _backoff((self.name, key)) is None:
                    self._refresh(key, path, fetch)
                return value
        error = _backoff((self.name, key))
        if error is not None:
            raise _bare(error)
        if allow_fetch is not None and not allow_fetch():
            raise Refused(key)
        return self._fetch_once(key, path, fetch)

    def _dir(self) -> Path | None:
        return None if _root is None else _root / self.name

    def _read(self, path: Path) -> tuple[float, T] | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            header, payload = raw.split(b"\n", 1)
            head = json.loads(header)
            return time.time() - float(head["t"]), self._decode(payload, head["i"])
        except (ValueError, KeyError, TypeError):
            logger.warning("Dropping an unreadable %s cache entry %s", self.name, path.name)
            with contextlib.suppress(OSError):
                path.unlink()
            return None

    def _fetch_once(self, key: str, path: Path, fetch: Callable[[], T]) -> T:
        flight = (self.name, key)
        future, owner = _claim(flight)
        if not owner:
            result: T = future.result()
            return result
        return self._fill(flight, future, path, fetch)

    def _fill(
        self, flight: tuple[str, str], future: Future[Any], path: Path, fetch: Callable[[], T]
    ) -> T:
        """Fetch into the claimed *future* and store the value; the future is always
        settled, so no waiter is left blocked."""
        try:
            value = fetch()
        except Exception as exc:
            with _guard:
                if len(_failed) >= _BACKOFF_MAX_KEYS:
                    _failed.clear()
                _failed[flight] = (time.monotonic() + BACKOFF_S, _bare(exc))
            future.set_exception(exc)
            raise
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(value)
            self._write(path, value)
            return value
        finally:
            with _guard:
                _inflight.pop(flight, None)

    def _refresh(self, key: str, path: Path, fetch: Callable[[], T]) -> None:
        flight = (self.name, key)
        # Claimed now, not when the pool gets to it, so stale hits while the refresh
        # waits in the queue do not queue more of them.
        future, owner = _claim(flight)
        if not owner:
            return

        def run() -> None:
            try:
                self._fill(flight, future, path, fetch)
            except Exception:
                logger.info("Background refresh of a %s cache entry failed", self.name)

        if sync:
            run()
            return
        try:
            _executor().submit(run)
        except RuntimeError as exc:  # the pool is shut down (interpreter exit)
            with _guard:
                _inflight.pop(flight, None)
            future.set_exception(exc)

    def _write(self, path: Path, value: T) -> None:
        payload, info = self._encode(value)
        header = json.dumps({"t": time.time(), "i": info}).encode()
        temp: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp") as f:
                temp = f.name
                f.write(header + b"\n" + payload)
            os.replace(temp, path)
            temp = None
        except OSError:
            logger.warning("Writing a %s cache entry failed", self.name, exc_info=True)
            if temp:
                with contextlib.suppress(OSError):
                    os.remove(temp)
            return
        self._maybe_sweep(path.parent)

    def _maybe_sweep(self, directory: Path) -> None:
        with _guard:
            self._writes += 1
            if self._writes < _SWEEP_EVERY:
                return
            self._writes = 0
        if not self._sweep_lock.acquire(blocking=False):
            return
        try:
            entries: list[tuple[float, int, Path]] = []
            tmp_cutoff = time.time() - _TMP_MAX_AGE_S
            for entry in directory.iterdir():
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if entry.suffix == ".tmp":
                    # Left behind by a write a crash cut short; a live one is seconds old.
                    if st.st_mtime < tmp_cutoff:
                        with contextlib.suppress(OSError):
                            entry.unlink()
                    continue
                entries.append((st.st_mtime, st.st_size, entry))
            total = sum(size for _, size, _ in entries)
            if total <= self.max_bytes:
                return
            target = int(self.max_bytes * 0.9)
            for _, size, entry in sorted(entries, key=lambda e: e[0]):
                if total <= target:
                    break
                with contextlib.suppress(OSError):
                    entry.unlink()
                    total -= size
        except OSError:
            logger.debug("Sweeping the %s cache failed", self.name, exc_info=True)
        finally:
            self._sweep_lock.release()


def _backoff(flight: tuple[str, str]) -> BaseException | None:
    with _guard:
        failed = _failed.get(flight)
        if failed is None:
            return None
        until, exc = failed
        if until > time.monotonic():
            return exc
        del _failed[flight]
        return None


def _claim(flight: tuple[str, str]) -> tuple[Future[Any], bool]:
    """The future of *flight* in progress, or a new one; True when the caller made it
    and so must settle it."""
    with _guard:
        future = _inflight.get(flight)
        if future is not None:
            return future, False
        future = _inflight[flight] = Future()
        return future, True


def _bare[E: BaseException](exc: E) -> E:
    """A copy of *exc* without its traceback, cause or context, so the backoff holds no
    frames of the request that failed and each raise gets a traceback of its own."""
    try:
        clone = copy.copy(exc)
    except TypeError:  # an __init__ that wants more than args: rebuild without calling it
        clone = type(exc).__new__(type(exc))
        clone.args = exc.args
        clone.__dict__.update(exc.__dict__)
    clone.__traceback__ = None
    clone.__cause__ = None
    clone.__context__ = None
    return clone


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _guard:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="disk-cache")
        return _pool


def _encode_json(value: Any) -> tuple[bytes, dict[str, Any]]:
    return json.dumps(value, separators=(",", ":")).encode(), {}


def _decode_json(payload: bytes, _info: dict[str, Any]) -> Any:
    return json.loads(payload)


def _encode_image(value: Image) -> tuple[bytes, dict[str, Any]]:
    return value


def _decode_image(payload: bytes, info: dict[str, Any]) -> Image:
    return payload, {str(k): str(v) for k, v in info.items()}


_META: Store[Any] = Store("meta", _META_MAX_BYTES, _encode_json, _decode_json)
_IMG: Store[Image] = Store("img", _IMG_MAX_BYTES, _encode_image, _decode_image)


def meta() -> Store[Any]:
    """The store for JSON values (TMDB lookups)."""
    return _META


def img() -> Store[Image]:
    """The store for images: values are ``(bytes, info)``."""
    return _IMG


def init(data_dir: str | Path) -> None:
    """Store entries under ``<data_dir>/watch_cache`` from now on, creating its folders."""
    global _root
    root = Path(data_dir) / "watch_cache"
    for store in (_META, _IMG):
        (root / store.name).mkdir(parents=True, exist_ok=True)
    _root = root


__all__ = ["BACKOFF_S", "Image", "Refused", "Store", "img", "init", "meta", "sync"]
