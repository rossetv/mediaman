"""Keep each linked person's plays up to date."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import requests

from mediaman.core.format import log_snippet
from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.db import open_thread_connection
from mediaman.services.infra import (
    allowed_outbound_hosts,
    get_setting,
    get_string_setting,
)
from mediaman.services.media_meta.plex_watch import PlexError
from mediaman.services.plex_link import repository
from mediaman.services.plex_link.history import PlexHistory

logger = logging.getLogger(__name__)

KEEP_FOR = timedelta(days=730)
KEEP_PLAYS = 2000
_OVERLAP = timedelta(hours=24)
_RUN_SECONDS = 300.0

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


def _read_link(
    conn: sqlite3.Connection, plex: PlexHistory, server_id: str, link: repository.PlexLink
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
    plays = plex.plays(
        link.server_account_id,
        since=oldest if last is None else max(oldest, last - _OVERLAP),
        now=now,
        libraries=_libraries(conn),
        limit=KEEP_PLAYS,
    )
    repository.save_plays(conn, link, plays, read_at=now, oldest=oldest, keep=KEEP_PLAYS)


def read_plays(conn: sqlite3.Connection, secret_key: str, *, username: str | None = None) -> None:
    """Read new plays of every linked person, or of *username* alone.

    Never raises for Plex or one person's trouble: their plays stay as they were.
    """
    links = [
        link
        for link in repository.list_links(conn)
        if username is None or link.username == username
    ]
    if not links:
        return
    try:
        plex = open_history(conn, secret_key)
        server_id = plex.server_id()
    except PlexError as exc:
        logger.warning("plex_link.read_skipped reason=%s", type(exc).__name__)
        return
    deadline = time.monotonic() + _RUN_SECONDS
    for link in links:
        if time.monotonic() > deadline:
            logger.warning("plex_link.read_stopped reason=time")
            return
        try:
            _read_link(conn, plex, server_id, link)
        except (PlexError, requests.RequestException, sqlite3.Error) as exc:
            logger.warning(
                "plex_link.read_failed user=%s reason=%s",
                log_snippet(link.username, 64),
                type(exc).__name__,
            )


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
