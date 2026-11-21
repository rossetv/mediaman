"""Filesystem cache helpers for the poster proxy.

Path safety: cache filenames are the full SHA-256 hash of the rating_key, so no
user-supplied string reaches the filesystem (no path traversal or filename injection).

Sidecar mime files: each image has a .mime sidecar recording the upstream Content-Type.
Without it cache hits would be served as image/jpeg, breaking PNG/WebP under
X-Content-Type-Options: nosniff. The sidecar is written atomically and swept with the image.

Atomic writes: image and sidecar use tempfile.NamedTemporaryFile plus os.replace, so readers
never see a partial write. If os.replace fails, callers must unlink the orphan temp file.

Module state: the cache directory and LRU sweep counter are module-level so one worker pays
the directory walk at most once per _CACHE_GC_RECHECK_EVERY writes. Tests reset _cache_dir
to None.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Cache posters for 7 days (response header) — browser won't re-request
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# Soft cap for the on-disk poster cache. Unbounded, a long-lived install
# would let it grow until the data volume filled. 500 MiB is generous (a poster averages ~80 KiB, so
# this caps at roughly 6,500 posters) but keeps disk pressure
# predictable. Cleanup is opportunistic: once total size exceeds the
# cap, the writer deletes oldest files by mtime until usage is back
# under the cap. The check is throttled so we don't pay the directory
# walk cost on every request.
_CACHE_DIR_MAX_BYTES = 500 * 1024 * 1024
#: One-in-N requests trigger a real LRU sweep. Rest path-checks just
#: see if the in-memory size estimate exceeds the cap.
_CACHE_GC_RECHECK_EVERY = 50

# Sweep state guarded by the lock so two concurrent requests don't both
# walk the directory. Lock is best-effort — if it cannot be acquired
# without blocking, the request skips the GC and serves immediately.
_cache_gc_lock = threading.Lock()
# rationale: _cache_gc_counter is incremented from the FastAPI thread pool
# (multiple concurrent poster requests); the dedicated lock guards the
# read-modify-write so no increments are lost to a torn update.
_cache_gc_counter_lock = threading.Lock()
_cache_gc_counter = 0

# rationale: single-worker, per-process cache root — guarded so two
# concurrent first requests cannot both see None and both mkdir.
_cache_dir_lock = threading.Lock()
#: The poster cache directory.  Populated on first request from the
#: app config.  Tests reset this to ``None`` between runs.
_cache_dir: Path | None = None


def get_cache_dir(data_dir: str) -> Path:
    """Return (and lazily create) the poster cache directory. The lifespan calls it at startup;
    the lazy guard keeps per-request calls cheap and stays safe in tests or CLI contexts
    that run before startup.
    """
    global _cache_dir
    if _cache_dir is None:
        with _cache_dir_lock:
            if _cache_dir is None:
                _cache_dir = Path(data_dir) / "poster_cache"
                _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _sweep_oldest(entries: list[tuple[float, int, Path]], total: int) -> None:
    entries.sort(key=lambda e: e[0])
    target = int(_CACHE_DIR_MAX_BYTES * 0.9)
    for _mtime, size, path in entries:
        if total <= target:
            break
        try:
            path.unlink()
            total -= size
            # Unlink the sidecar unconditionally — the pattern
            # ``<name>.<ext>.mime`` applies to any extension so future
            # WebP / PNG entries are covered without an extension guard.
            sidecar = path.with_suffix(path.suffix + ".mime")
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass  # sidecar already gone — nothing to sweep
            except OSError:
                logger.debug("Failed to unlink sidecar for %s", path, exc_info=True)
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("Failed to unlink %s during sweep", path, exc_info=True)


def _bump_gc_counter() -> bool:
    global _cache_gc_counter
    with _cache_gc_counter_lock:
        _cache_gc_counter += 1
        if _cache_gc_counter < _CACHE_GC_RECHECK_EVERY:
            return False
    return True


def maybe_sweep_cache(cache_dir: Path) -> None:
    """Opportunistically delete oldest cache entries (by mtime) until the total is under 90% of
    _CACHE_DIR_MAX_BYTES.

    The walk holds a non-blocking lock so concurrent writers do not stampede the directory
    (another sweeper means skip), and a throttled counter runs it once per
    _CACHE_GC_RECHECK_EVERY writes.
    """
    if not _bump_gc_counter():
        return
    if not _cache_gc_lock.acquire(blocking=False):
        return
    # Reset the counter under its own lock so concurrent increments from
    # other threads (which only acquire _cache_gc_counter_lock) cannot tear
    # the reset.  The GC lock is already held here, preventing a second
    # concurrent sweep from racing the counter reset.
    with _cache_gc_counter_lock:
        global _cache_gc_counter
        _cache_gc_counter = 0
    try:
        entries: list[tuple[float, int, Path]] = []
        total = 0
        try:
            for entry in cache_dir.iterdir():
                # ``.tmp`` files are short-lived from in-flight writes —
                # don't touch them here. Sidecars are tiny and walk-
                # cheap; sweep them alongside their image so we don't
                # leak orphans.
                try:
                    st = entry.stat()
                except OSError:
                    continue
                size = int(st.st_size)
                total += size
                entries.append((st.st_mtime, size, entry))
        except OSError:
            return

        if total <= _CACHE_DIR_MAX_BYTES:
            return

        _sweep_oldest(entries, total)
    finally:
        _cache_gc_lock.release()


def write_poster_cache(
    cache_dir: Path,
    cached_path: Path,
    rating_key: str,
    content: bytes,
    content_type: str,
) -> None:
    """Write poster content atomically (temp file in the same directory, then os.replace), then
    trigger an LRU sweep. On failure the temp file is removed explicitly, or it would orphan
    disk space until the next sweep.
    """
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False, suffix=".tmp") as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        os.replace(temp_path, cached_path)
        temp_path = None  # Success — replace consumed the temp.
        # Persist the served mime so subsequent cache hits return the
        # same Content-Type rather than always claiming image/jpeg.
        write_sidecar_mime(cached_path, content_type)
        # Opportunistic LRU sweep — if the cache directory has grown
        # past the soft cap, delete oldest entries until back under.
        maybe_sweep_cache(cache_dir)
    except OSError:
        logger.warning("Poster cache write failed for %s", rating_key, exc_info=True)
        if temp_path:
            with contextlib.suppress(OSError):
                os.remove(temp_path)


# Only these mime types are ever served back to the client.  Everything
# else is normalised down to ``image/jpeg`` so a malicious upstream CDN
# cannot serve ``Content-Type: text/html`` through the proxy and land a
# stored-XSS-via-poster primitive.
ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)


def read_sidecar_mime(cache_path: Path) -> str:
    """Return the mime type recorded for a cached poster, or image/jpeg.

    Falls back to image/jpeg when the sidecar (<cache_path>.mime) is missing (legacy
    entries), empty or over 64 bytes (malformed write), or names a type outside
    ALLOWED_IMAGE_MIMES. Sidecar content is never trusted unconditionally: a compromised
    cache write could otherwise inject a hostile Content-Type.
    """
    sidecar = cache_path.with_suffix(cache_path.suffix + ".mime")
    try:
        raw = sidecar.read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return "image/jpeg"
    if not raw or len(raw) > 64:
        return "image/jpeg"
    if raw not in ALLOWED_IMAGE_MIMES:
        return "image/jpeg"
    return raw


def write_sidecar_mime(cache_path: Path, mime: str) -> None:
    """Atomically persist the served mime type alongside cache_path (temp file in the same
    directory, then rename). Failure is non-fatal: the next cache hit falls back to
    image/jpeg. A mime outside ALLOWED_IMAGE_MIMES is stored as image/jpeg, so an unchecked
    upstream header can never put a hostile value in the sidecar.
    """
    sidecar = cache_path.with_suffix(cache_path.suffix + ".mime")
    safe = mime if mime in ALLOWED_IMAGE_MIMES else "image/jpeg"
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent, delete=False, suffix=".mime.tmp"
        ) as temp_file:
            temp_file.write(safe.encode("ascii"))
            temp_path = temp_file.name
        os.replace(temp_path, sidecar)
    except OSError:
        logger.debug("Sidecar mime write failed for %s", cache_path, exc_info=True)
        if temp_path:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
