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
for keep links. The in-memory store is a small fast-path negative cache
populated *after* the DB has spoken; it never makes a unilateral claim
without DB confirmation, so the cache is always consistent with the
authoritative table.

The cache is a bounded LRU (:class:`collections.OrderedDict`) so the
process memory cannot grow unbounded under sustained load with
14-day-TTL tokens. Expired entries are pruned opportunistically; the
public :func:`gc_expired_tokens` should also be wired into a startup or
scheduled job so low-volume instances eventually purge expired rows
even when the cache never fills.
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
    """Return the SHA-256 hex digest of *token* — the at-rest identifier."""
    return hashlib.sha256(token.encode()).hexdigest()


def reset_used_tokens() -> None:
    """Clear the in-memory replay cache. Tests use this to isolate cases.

    The DB-backed ``used_download_tokens`` table is owned by whichever
    fixture provisioned the connection — tests that want a fresh DB
    state simply provision a fresh connection. The in-memory LRU is
    process-wide module state, so it must be wiped explicitly.
    """
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.clear()


def _get_db_or_none() -> sqlite3.Connection | None:
    """Return the active DB connection, or ``None`` if uninitialised.

    The token store falls back to memory-only mode in tests that bypass
    :func:`mediaman.db.init_db` (e.g. the ``_tokens`` unit tests). In
    production both ``init_db`` and ``set_connection`` always run during
    the lifespan startup hook.
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
    """Atomically claim *digest* in the DB. Returns ``True`` on first claim.

    Thin adaptor around :func:`claim_download_token` — kept as a
    private hook so the in-memory LRU cache path can mock the DB
    interaction in tests.
    """
    return claim_download_token(conn, digest=digest, exp=exp)


def _release_used_token(conn: sqlite3.Connection, digest: str) -> None:
    """Delete *digest* from the persistent claim store, swallowing failures.

    Called on the failure paths (Radarr/Sonarr unreachable, etc.) so the
    user can retry. A best-effort delete: a transient DB lock during
    release is logged but not raised — the next successful run cleans up
    the row anyway.
    """
    try:
        release_download_token(conn, digest)
    except sqlite3.Error:
        logger.exception("download token release failed for digest=%s", digest)


def gc_expired_tokens(conn: sqlite3.Connection | None = None) -> None:
    """Purge expired rows from ``used_download_tokens``.

    Public entry point intended to be wired into the startup or
    scheduled-job pipeline (see ``mediaman.bootstrap.scheduling``) so
    expired rows are cleaned up even on low-volume instances where the
    in-memory cache never fills (and therefore never trips the
    opportunistic GC pass in :func:`_mark_token_used`). Without that
    wiring, ``used_download_tokens`` would grow without bound on a
    quiet instance because the only existing GC trigger requires the
    cache to overflow.

    Pass *conn* explicitly when calling outside a request scope (e.g.
    from a scheduler thread that has its own connection); otherwise
    the active per-request connection is used.
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
    """Trim the LRU cache to within :data:`_TOKEN_USED_CACHE_MAX`.

    Caller must already hold :data:`_USED_TOKENS_LOCK`. Two-pass:

    1. First drop any entries whose ``exp`` has elapsed — those are
       cheap wins that don't lose any meaningful state.
    2. If the cache is still over the cap, evict in insertion order
       (the LRU end of the ``OrderedDict``). This bounds memory on a
       sustained-load instance with 14-day-TTL tokens that would
       otherwise pile up indefinitely. An evicted entry is harmless —
       the next request for that token will miss the cache and consult
       the DB, which is still authoritative.
    """
    now = time.time()
    for k, v in list(_USED_TOKENS.items()):
        if v < now:
            _USED_TOKENS.pop(k, None)
    while len(_USED_TOKENS) > _TOKEN_USED_CACHE_MAX:
        _USED_TOKENS.popitem(last=False)


def _mark_token_used(token: str, exp: int) -> bool:
    """Atomically mark *token* as consumed. Return ``False`` if already used.

    The DB is the source of truth. The in-memory cache is a bounded
    fast-path negative cache populated *after* the DB call returns, so
    it cannot make a claim the DB doesn't also know about.

    Order:

    1. Cheap in-process check: if the digest is already in the cache,
       return ``False`` — this is a confirmed replay (the cache only
       holds digests the DB has previously seen).
    2. ``INSERT OR IGNORE`` against ``used_download_tokens``. The
       rowcount tells us whether this is the first claim or someone
       else got there first.
    3. Either way, populate the cache with the digest so future
       requests can short-circuit step 2.

    On DB failure during step 2 the function logs CRITICAL and
    re-raises. The caller (the submit handler) translates that into a
    503 so the operator can retry once the DB recovers — failing closed
    is preferable to letting a replay sneak through on the optimistic
    cache write.
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
    """Release a previously claimed token so the user can retry."""
    digest = _digest(token)
    with _USED_TOKENS_LOCK:
        _USED_TOKENS.pop(digest, None)
    conn = _get_db_or_none()
    if conn is not None:
        _release_used_token(conn, digest)
