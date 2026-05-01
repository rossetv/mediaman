"""Persistent single-use store for download confirmation tokens.

Originally an in-memory dict keyed by SHA-256 digest. That broke under
two operational realities:

* A process restart cleared the cache, so the same one-shot link could
  be replayed on the freshly booted instance.
* A multi-worker uvicorn deployment kept one cache per worker, so a
  link could trivially be replayed against a sibling worker.

The fix is to persist consumed token hashes in SQLite under a unique
constraint (table ``used_download_tokens``, migration 23). The DB
``INSERT`` is the authoritative claim — equivalent to ``keep_tokens_used``
for keep links. The legacy in-memory dict is retained as a tiny fast-path
cache so the ``test__tokens.py`` regression suite keeps working without a
DB connection, but every real claim now goes through SQLite.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("mediaman")

_USED_TOKENS_LOCK = threading.Lock()
_USED_TOKENS: dict[str, float] = {}

# Maximum number of consumed tokens held in the in-memory cache before
# an eviction pass removes expired entries. 1 000 is generous enough to
# handle burst usage without unbounded growth on a busy instance.
_TOKEN_USED_CACHE_MAX = 1000


def _digest(token: str) -> str:
    """Return the SHA-256 hex digest of *token* — the at-rest identifier."""
    return hashlib.sha256(token.encode()).hexdigest()


def _get_db_or_none() -> sqlite3.Connection | None:
    """Return the active DB connection, or ``None`` if uninitialised.

    The token store falls back to memory-only mode in tests that bypass
    :func:`mediaman.db.init_db` (e.g. the ``_tokens`` unit tests). In
    production both ``init_db`` and ``set_connection`` always run during
    the lifespan startup hook.
    """
    try:
        from mediaman.db import get_db
    except Exception:
        return None
    try:
        return get_db()
    except Exception:
        return None


def _persist_used_token(conn: sqlite3.Connection, digest: str, exp: int) -> bool:
    """Atomically claim *digest* in the DB. Returns ``True`` on first claim.

    Uses ``INSERT OR IGNORE`` against the unique-keyed
    ``used_download_tokens`` table — the rowcount tells us whether this
    claim succeeded or whether a sibling worker / earlier request had
    already taken the slot.
    """
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    used_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO used_download_tokens "
        "(token_hash, expires_at, used_at) VALUES (?, ?, ?)",
        (digest, expires_at, used_at),
    )
    conn.commit()
    return cursor.rowcount == 1


def _release_used_token(conn: sqlite3.Connection, digest: str) -> None:
    """Delete *digest* from the persistent claim store, swallowing failures.

    Called on the failure paths (Radarr/Sonarr unreachable, etc.) so the
    user can retry. A best-effort delete: a transient DB lock during
    release is logged but not raised — the next successful run cleans up
    the row anyway.
    """
    try:
        conn.execute("DELETE FROM used_download_tokens WHERE token_hash = ?", (digest,))
        conn.commit()
    except Exception:
        logger.warning("download token release failed for digest=%s", digest, exc_info=True)


def _gc_expired_tokens(conn: sqlite3.Connection) -> None:
    """Drop expired rows from ``used_download_tokens``.

    Runs opportunistically when the in-memory cache exceeds its size
    cap. The token TTL is short (set by the issuer; typically ~30
    days) so the table stays small even without an explicit job.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("DELETE FROM used_download_tokens WHERE expires_at < ?", (now_iso,))
        conn.commit()
    except Exception:
        logger.debug("download token GC failed", exc_info=True)


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark *token* as consumed. Return ``False`` if already used.

    Order:

    1. Cheap in-process check + populate. Two concurrent requests in the
       same worker collide here without a DB round-trip.
    2. DB ``INSERT OR IGNORE``. Two concurrent requests across workers
       (or a request after a process restart) collide here.

    Both layers must succeed for the claim to count. If the DB write
    rejects (someone else got the row first), we also rewind the
    in-memory cache so the user is told "already used" rather than
    seeing a stale local cache hit.
    """
    digest = _digest(token)
    now = time.time()
    with _USED_TOKENS_LOCK:
        if len(_USED_TOKENS) > _TOKEN_USED_CACHE_MAX:
            for k, v in list(_USED_TOKENS.items()):
                if v < now:
                    _USED_TOKENS.pop(k, None)
            conn = _get_db_or_none()
            if conn is not None:
                _gc_expired_tokens(conn)
        if digest in _USED_TOKENS:
            return False
        _USED_TOKENS[digest] = float(exp)

    conn = _get_db_or_none()
    if conn is None:
        # No DB available (test harness). The in-memory cache alone is
        # the authoritative store in this mode.
        return True

    try:
        claimed = _persist_used_token(conn, digest, exp)
    except Exception:
        # DB failure on the authoritative claim path — refuse the token
        # rather than letting a replay slip through. Roll back the
        # cache entry so a retry can succeed once the DB recovers.
        logger.warning("download token persistence failed; refusing claim", exc_info=True)
        with _USED_TOKENS_LOCK:
            _USED_TOKENS.pop(digest, None)
        return False

    if not claimed:
        # Another worker claimed the same token — make sure local state
        # agrees with the DB so the user sees the consistent "already used"
        # response on subsequent retries against this worker.
        with _USED_TOKENS_LOCK:
            _USED_TOKENS[digest] = float(exp)
        return False

    return True


def _unmark_token_used(token: str) -> None:
    """Release a previously claimed token so the user can retry."""
    digest = _digest(token)
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.pop(digest, None)
    conn = _get_db_or_none()
    if conn is not None:
        _release_used_token(conn, digest)
