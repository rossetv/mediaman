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
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("mediaman")

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
