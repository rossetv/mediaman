"""Filesystem cache helpers for the poster proxy.

Posters are cached to ``MEDIAMAN_DATA_DIR/poster_cache/`` on first fetch
and served from disk on subsequent requests, avoiding repeated round-trips
to Plex or TMDB CDN.

Path safety
-----------
Cache filenames are derived from the Plex ``rating_key`` via a full
SHA-256 hash (64 hex characters), so no user-supplied string ever lands
directly on the filesystem.  This eliminates path-traversal and filename-
injection risks regardless of the rating_key format.

Sidecar mime files
------------------
Each cached image is accompanied by a ``.mime`` sidecar file that records
the upstream ``Content-Type``.  Without this, cache hits would always be
served as ``image/jpeg`` — breaking PNG/WebP entries under
``X-Content-Type-Options: nosniff``.  The sidecar is written atomically
alongside the image and is swept with it during LRU eviction.

Atomic writes
-------------
Both the image and the sidecar are written via ``tempfile.NamedTemporaryFile``
+ ``os.replace``.  This guarantees that concurrent readers never see a
partial write.  If ``os.replace`` fails after the temp file has been
created, callers are responsible for unlinking the orphan temp file.

Module state
------------
The cache directory and the LRU sweep counter are module-level so a
single FastAPI worker pays the directory walk at most once per
``_CACHE_GC_RECHECK_EVERY`` writes.  Tests reset ``_cache_dir`` to
``None`` between runs.
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

# Soft cap for the on-disk poster cache. The directory was previously
# unbounded — a long-lived install would let it grow until the data
# volume filled. 500 MiB is generous (a poster averages ~80 KiB, so
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

#: The poster cache directory.  Populated on first request from the
#: app config.  Tests reset this to ``None`` between runs.
_cache_dir: Path | None = None


def get_cache_dir(data_dir: str) -> Path:
    """Return (and lazily create) the poster cache directory.

    The lifespan in ``main.py`` calls this at startup so the directory
    exists before any request arrives. The lazy-init guard keeps subsequent
    per-request calls cheap (a module-level attribute check rather than a
    filesystem stat) while remaining safe if called before startup completes
    in tests or CLI contexts.
    """
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(data_dir) / "poster_cache"
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def _sweep_oldest(entries: list[tuple[float, int, Path]], total: int) -> None:
    """Delete oldest entries until total bytes drop under 90% of the cap."""
    entries.sort(key=lambda e: e[0])
    target = int(_CACHE_DIR_MAX_BYTES * 0.9)
    for _mtime, size, path in entries:
        if total <= target:
            break
        try:
            path.unlink()
            total -= size
            # If we removed an image, also unlink its sidecar.
            if path.suffix == ".jpg":
                sidecar = path.with_suffix(path.suffix + ".mime")
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug("Failed to unlink sidecar for %s", path, exc_info=True)
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("Failed to unlink %s during sweep", path, exc_info=True)


def _bump_gc_counter() -> bool:
    """Increment the throttled-sweep counter and return True when it tripped."""
    global _cache_gc_counter
    with _cache_gc_counter_lock:
        _cache_gc_counter += 1
        if _cache_gc_counter < _CACHE_GC_RECHECK_EVERY:
            return False
    return True


def maybe_sweep_cache(cache_dir: Path) -> None:
    """Opportunistically delete oldest cache entries when over the cap.

    Called from the cache-write path. The sweep walks the directory
    once, sums up sizes, and — if total exceeds
    :data:`_CACHE_DIR_MAX_BYTES` — deletes oldest-mtime files until the
    total is back under 90% of the cap.

    The walk is guarded by a non-blocking lock so concurrent writers
    don't stampede the same directory; if another thread is already
    sweeping, we skip and return.  The throttled counter means the
    expensive walk only runs once per ``_CACHE_GC_RECHECK_EVERY`` writes.
    """
    global _cache_gc_counter
    if not _bump_gc_counter():
        return
    if not _cache_gc_lock.acquire(blocking=False):
        return
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
    """Write poster content to disk atomically, then trigger an LRU sweep.

    Writes to a temp file in the same directory (guaranteeing same
    filesystem), then ``os.replace`` into place so another reader cannot
    see a partial write.  On failure after the temp file was written,
    the temp must be removed explicitly — leaving it would orphan disk
    space until the next sweep.
    """
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False, suffix=".tmp") as tmp:
            tmp.write(content)
            tmp_name = tmp.name
        os.replace(tmp_name, cached_path)
        tmp_name = None  # Success — replace consumed the temp.
        # Persist the served mime so subsequent cache hits return the
        # same Content-Type rather than always claiming image/jpeg.
        write_sidecar_mime(cached_path, content_type)
        # Opportunistic LRU sweep — if the cache directory has grown
        # past the soft cap, delete oldest entries until back under.
        maybe_sweep_cache(cache_dir)
    except OSError:
        logger.warning("Poster cache write failed for %s", rating_key, exc_info=True)
        if tmp_name:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)


# Only these mime types are ever served back to the client.  Everything
# else is normalised down to ``image/jpeg`` so a malicious upstream CDN
# cannot serve ``Content-Type: text/html`` through the proxy and land a
# stored-XSS-via-poster primitive.
ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)


def read_sidecar_mime(cache_path: Path) -> str:
    """Return the mime type recorded for a cached poster, or ``image/jpeg``.

    The sidecar file lives at ``<cache_path>.mime`` (e.g.
    ``abc123.jpg.mime``) and contains a plain ASCII string such as
    ``image/png``.

    Falls back to ``image/jpeg`` when the sidecar:

    * does not exist (legacy entries written before sidecar support),
    * is empty or longer than 64 bytes (malformed / truncated write),
    * names a mime type that is not in :data:`ALLOWED_IMAGE_MIMES`
      (defensive: never trust sidecar content unconditionally — a
      compromised cache write could otherwise inject a hostile
      Content-Type).

    Args:
        cache_path: Absolute path to the cached image file.

    Returns:
        A safe mime type string, always one of :data:`ALLOWED_IMAGE_MIMES`
        or ``"image/jpeg"``.
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
    """Atomically persist the served mime type alongside *cache_path*.

    Mirrors the temp-file + ``os.replace`` pattern used for the image
    bytes.  The sidecar is written to a ``.mime.tmp`` temp file in the
    same directory (guaranteeing same filesystem), then renamed into place.

    Failure is non-fatal: if the sidecar cannot be written, the next
    cache hit falls back to ``image/jpeg``, which is the safe default.
    Callers should not raise on failure from this function.

    Args:
        cache_path: Absolute path to the cached image file.
        mime:       The mime type to persist.  If not in
                    :data:`ALLOWED_IMAGE_MIMES`, ``"image/jpeg"`` is stored
                    instead — the allow-list is enforced at write time so
                    the sidecar can never contain a hostile value even if
                    the caller passes through an unchecked upstream header.
    """
    sidecar = cache_path.with_suffix(cache_path.suffix + ".mime")
    safe = mime if mime in ALLOWED_IMAGE_MIMES else "image/jpeg"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent, delete=False, suffix=".mime.tmp"
        ) as tmp:
            tmp.write(safe.encode("ascii"))
            tmp_name = tmp.name
        os.replace(tmp_name, sidecar)
    except OSError:
        logger.debug("Sidecar mime write failed for %s", cache_path, exc_info=True)
        if tmp_name:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
