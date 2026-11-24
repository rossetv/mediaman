"""Keep each linked person's plays up to date."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from mediaman.core.format import log_snippet
from mediaman.core.time import now_iso, now_utc, parse_iso_strict_utc
from mediaman.db import open_thread_connection
from mediaman.services.infra import (
    allowed_outbound_hosts,
    get_setting,
    get_string_setting,
)
from mediaman.services.media_meta.plex_watch import PlexError, PlexUnavailable
from mediaman.services.plex_link import repository
from mediaman.services.plex_link.bounded import PLEX_SERVER, TooBusy, TookTooLong
from mediaman.services.plex_link.history import PlexHistory

logger = logging.getLogger(__name__)

KEEP_FOR = timedelta(days=730)
KEEP_PLAYS = 2000
_OVERLAP = timedelta(hours=24)
_RUN_SECONDS = 300.0
_PERSON_SECONDS = 60.0
_SERVER_ID_SECONDS = 20.0

_reading: set[str] = set()
_reading_lock = threading.Lock()


def open_history(conn: sqlite3.Connection, secret_key: str) -> PlexHistory:
    """A reader for the configured server; raises PlexNotConfigured when there is none."""
    return PlexHistory(
        get_string_setting(conn, "plex_url"),
        get_string_setting(conn, "plex_token", secret_key=secret_key),
        allowed_hosts=allowed_outbound_hosts(conn),
    )


def _libraries(conn: sqlite3.Connection) -> set[str]:
    libs = get_setting(conn, "plex_libraries", default=[])
    return {str(v) for v in libs} if isinstance(libs, list) else set()


def read_server_id(plex: PlexHistory) -> str:
    """Which server *plex* is; raises PlexUnavailable when it does not say in time."""
    try:
        return PLEX_SERVER.within(_SERVER_ID_SECONDS, plex.server_id)
    except (TookTooLong, TooBusy):
        raise PlexUnavailable("Plex took too long to answer") from None


def _read_link(
    conn: sqlite3.Connection,
    plex: PlexHistory,
    server_id: str,
    link: repository.PlexLink,
    libraries: set[str],
    deadline: float,
) -> None:
    if link.server_id != server_id:
        # Account numbers mean nothing on another server.
        repository.unlink(
            conn,
            link.username,
            actor="",
            reason="server_changed",
            server_id=link.server_id,
        )
        return
    now = now_utc()
    oldest = now - KEEP_FOR
    last: datetime | None = parse_iso_strict_utc(link.last_read_at)
    since = oldest if last is None else max(oldest, last - _OVERLAP)
    if link.libraries is None or not libraries <= link.libraries:
        # A library added since has older plays that were passed over.
        since = oldest
    try:
        plays = PLEX_SERVER.within(
            deadline - time.monotonic(),
            lambda: plex.plays(
                link.server_account_id,
                since=since,
                now=now,
                libraries=libraries,
                limit=KEEP_PLAYS,
                deadline=deadline,
            ),
        )
    except TookTooLong:
        raise PlexUnavailable("Plex took too long to send the plays") from None
    if not plays.complete:
        # Read from the same time again, or the plays further back would never be asked for.
        logger.warning("plex_link.read_incomplete user=%s", log_snippet(link.username, 64))
    repository.save_plays(
        conn,
        link,
        plays.found,
        libraries=libraries,
        read_at=now if plays.complete else None,
        oldest=oldest,
        keep=KEEP_PLAYS,
    )


def read_plays(conn: sqlite3.Connection, secret_key: str, *, username: str | None = None) -> None:
    """Read new plays of every linked person, or of *username* alone.

    Never raises: what cannot be read stays as it was. No transaction may be open on *conn*.
    """
    try:
        _read_all(conn, secret_key, username)
    except Exception as exc:  # rationale: a caller's own work must not fail for this.
        _give_up(conn, "plex_link.read_skipped", "", exc)


def _give_up(conn: sqlite3.Connection, event: str, username: str, exc: Exception) -> None:
    if conn.in_transaction:
        conn.rollback()
    if isinstance(exc, PlexError | sqlite3.Error):
        logger.warning("%s user=%s reason=%s", event, log_snippet(username, 64), type(exc).__name__)
    else:
        logger.exception("%s user=%s", event, log_snippet(username, 64))


def _read_all(conn: sqlite3.Connection, secret_key: str, username: str | None) -> None:
    if not repository.list_links(conn):
        return
    libraries = _libraries(conn)
    repository.keep_libraries(conn, libraries)
    links = [
        link
        for link in repository.list_links(conn)
        if username is None or link.username == username
    ]
    if not libraries or not links:
        return
    deadline = time.monotonic() + _RUN_SECONDS
    server_id = read_server_id(open_history(conn, secret_key))
    for link in links:
        now = time.monotonic()
        if now > deadline:
            logger.warning("plex_link.read_stopped reason=time")
            return
        repository.set_tried(conn, link.username, now_iso())
        try:
            # A reader of its own: one left behind may still be using the last person's.
            plex = open_history(conn, secret_key)
            person_deadline = min(deadline, now + _PERSON_SECONDS)
            _read_link(conn, plex, server_id, link, libraries, person_deadline)
        except TooBusy:
            repository.set_tried(conn, link.username, link.last_tried_at)
            logger.warning("plex_link.read_stopped reason=busy")
            return
        except Exception as exc:  # rationale: one person's trouble must not stop the next.
            _give_up(conn, "plex_link.read_failed", link.username, exc)


def is_reading(username: str) -> bool:
    with _reading_lock:
        return username in _reading


def read_in_background(db_path: str, secret_key: str, username: str) -> None:
    """Start the first read of a link just made; a second call while it runs does nothing."""
    with _reading_lock:
        if username in _reading:
            return
        _reading.add(username)

    def run() -> None:
        try:
            conn = open_thread_connection(db_path)
            try:
                read_plays(conn, secret_key, username=username)
            finally:
                conn.close()
        except Exception:  # rationale: a background read must not die without a trace.
            logger.exception("plex_link.background_read_failed")
        finally:
            with _reading_lock:
                _reading.discard(username)

    threading.Thread(target=run, name="plex-link-read", daemon=True).start()
