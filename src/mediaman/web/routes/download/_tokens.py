"""Persistent single-use store for download confirmation tokens.

Consumed token hashes live in SQLite (table used_download_tokens) under a unique constraint.
The DB INSERT is the authoritative claim, so a link cannot be replayed after a restart or
against a sibling worker. The in-memory bounded LRU is only a negative cache filled after
the DB has confirmed a claim. gc_expired_tokens should run periodically so quiet instances
purge expired rows even when the cache never fills.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import sqlite3
import threading
import time

from mediaman.core.time import now_iso as _now_iso
from mediaman.web.repository.download import (
    claim_download_token,
    download_token_claimed,
    purge_expired_download_tokens,
    release_download_token,
)

logger = logging.getLogger(__name__)

_USED_TOKENS_LOCK = threading.Lock()
#: Bounded LRU mapping ``digest -> exp_ts`` of recently-claimed tokens.
#: Acts as a fast-path negative cache: a hit short-circuits the DB
#: round-trip on the replay path. Entries are added only after the DB
#: has confirmed the claim outcome, so the cache can never make a
#: claim the DB has not also recorded.
_USED_TOKENS: collections.OrderedDict[str, float] = collections.OrderedDict()

#: Maximum entries held in the in-memory LRU cache. Generous enough to
#: absorb a normal burst yet small enough that the upper bound on
#: memory is trivial (a few hundred KiB even at full).
_TOKEN_USED_CACHE_MAX = 1000


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def reset_used_tokens() -> None:
    """Clear the in-memory replay cache (process-wide state; tests call this to isolate cases)."""
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.clear()


def _get_db_or_none() -> sqlite3.Connection | None:
    """Return the active DB connection, or None when uninitialised (tests that bypass init_db
    fall back to memory-only mode).
    """
    try:
        from mediaman.db import get_db
    except ImportError:
        return None
    try:
        return get_db()
    except RuntimeError:
        return None


def _persist_used_token(conn: sqlite3.Connection, digest: str, exp: int) -> bool:
    """Atomically claim digest in the DB; True on first claim. A private hook so tests can mock
    the DB interaction.
    """
    return claim_download_token(conn, digest=digest, exp=exp)


def _release_used_token(conn: sqlite3.Connection, digest: str) -> None:
    """Delete digest from the claim store, swallowing failures: a transient lock is logged, and
    the next successful run cleans the row up.
    """
    try:
        release_download_token(conn, digest)
    except sqlite3.Error:
        logger.exception("download token release failed for digest=%s", digest)


def gc_expired_tokens(conn: sqlite3.Connection | None = None) -> None:
    """Purge expired rows from used_download_tokens.

    Without a periodic call the table grows unbounded on quiet instances, where the cache
    never overflows to trigger the opportunistic GC. Pass conn when calling outside a
    request scope (e.g. a scheduler thread).
    """
    if conn is None:
        conn = _get_db_or_none()
        if conn is None:
            return
    now_iso = _now_iso()
    try:
        purge_expired_download_tokens(conn, now_iso=now_iso)
    except sqlite3.Error:
        logger.debug("download token GC failed", exc_info=True)


def _evict_cache_locked() -> None:
    """Trim the LRU cache to _TOKEN_USED_CACHE_MAX; caller must hold _USED_TOKENS_LOCK. An
    evicted entry is harmless: a miss falls back to the DB.
    """
    now = time.time()
    for k, v in list(_USED_TOKENS.items()):
        if v < now:
            _USED_TOKENS.pop(k, None)
    while len(_USED_TOKENS) > _TOKEN_USED_CACHE_MAX:
        _USED_TOKENS.popitem(last=False)


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark token consumed; False if already used. On DB failure this logs CRITICAL
    and re-raises: failing closed beats letting a replay through on an unverified cache
    write.
    """
    digest = _digest(token)
    with _USED_TOKENS_LOCK:
        if digest in _USED_TOKENS:
            # Confirmed replay: cache only holds entries the DB has
            # already seen, so this is a fast-path "no" without the DB
            # round-trip.
            _USED_TOKENS.move_to_end(digest)
            return False
        # GC pass: only pays the cost when the cache is at capacity, so
        # the typical request still pays nothing.
        if len(_USED_TOKENS) >= _TOKEN_USED_CACHE_MAX:
            _evict_cache_locked()

    conn = _get_db_or_none()
    if conn is None:
        # No DB available (test harness). The in-memory cache alone is
        # the authoritative store in this mode.
        with _USED_TOKENS_LOCK:
            _USED_TOKENS[digest] = float(exp)
            _USED_TOKENS.move_to_end(digest)
        return True

    try:
        claimed = _persist_used_token(conn, digest, exp)
    except sqlite3.Error:
        # DB failure on the authoritative claim path — refuse the
        # claim outcome rather than letting a replay slip through on
        # an unverified cache write. The caller translates the
        # exception into a 503 so the user can retry once the DB
        # recovers; legitimate first-use is briefly inconvenient,
        # which is the lesser of the two evils.
        logger.critical(
            "download token persistence failed; failing closed (raising)", exc_info=True
        )
        raise

    # Either we won the race or we lost it — either way the DB is now
    # authoritative for this digest, and a sibling cache miss should
    # not pay another DB round-trip.
    with _USED_TOKENS_LOCK:
        _USED_TOKENS[digest] = float(exp)
        _USED_TOKENS.move_to_end(digest)

    return claimed


def _unmark_token_used(token: str) -> None:
    digest = _digest(token)
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.pop(digest, None)
    conn = _get_db_or_none()
    if conn is not None:
        _release_used_token(conn, digest)


def is_token_used(conn: sqlite3.Connection, token: str) -> bool:
    return download_token_claimed(conn, _digest(token))


def _unanswered_digest(token: str) -> str:
    return _digest(f"unanswered add:{token}")


def note_unanswered_add(conn: sqlite3.Connection, token: str, exp: int) -> None:
    """Remember, until the link expires, that its add got no answer and may land later."""
    claim_download_token(conn, digest=_unanswered_digest(token), exp=exp)


def add_went_unanswered(conn: sqlite3.Connection, token: str) -> bool:
    """Whether an earlier add of this link got no answer."""
    return download_token_claimed(conn, _unanswered_digest(token))
