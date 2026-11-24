"""A write that fails must not leave its transaction open on the shared connection.

An open transaction holds the write lock, so the next writer on the process
(the scheduler, a request) waits on a lock nobody will release. Each case seeds
a row, makes the one write refuse, and checks the connection is free after.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from mediaman.services.arr._throttle_persistence import (
    _save_trigger_to_db,
    clear_throttle,
    reconcile_stranded_throttle,
)
from mediaman.services.arr.completion._sync import (
    _sync_recent_row,
    cleanup_recent_downloads,
)
from mediaman.services.arr.completion._verification import _batch_insert_completions
from mediaman.services.downloads._notification_claims import (
    ClaimedNotificationRow,
    _claim_pending_notifications,
    _release_claim,
    _release_claims_bulk,
    reconcile_stranded_notifications,
)
from mediaman.services.downloads.notifications import check_download_notifications
from mediaman.web.auth.login_lockout import record_failure, record_success
from mediaman.web.auth.password_hash import set_user_email
from mediaman.web.auth.password_hash._user_crud import set_must_change_password
from mediaman.web.repository.download import (
    claim_download_token,
    purge_expired_download_tokens,
    release_download_token,
)
from mediaman.web.repository.search import upsert_ratings_cache
from tests.helpers.factories import (
    insert_admin_user,
    insert_download_notification,
    insert_settings,
)

_OLD = "2000-01-01T00:00:00+00:00"


def _refuse(event: str, table: str, when: str = "") -> str:
    return (
        f"CREATE TRIGGER refuse {event} ON {table} {when} BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )


def _seed(conn, sql: str, params: tuple = ()) -> None:
    conn.execute(sql, params)
    conn.commit()


def _seed_token(conn):
    _seed(conn, "INSERT INTO used_download_tokens VALUES ('d', ?, ?)", (_OLD, _OLD))


def _seed_notification(conn, notified=2):
    return insert_download_notification(conn, notified=notified, claimed_at=_OLD)


def _claimed_notice():
    return ClaimedNotificationRow(
        id=1,
        email="user@example.com",
        title="Test Movie",
        media_type="movie",
        tmdb_id=None,
        tvdb_id=None,
        service="radarr",
        claimed_at=_OLD,
    )


def _seed_throttle(conn):
    _seed(conn, "INSERT INTO arr_search_throttle VALUES ('radarr:1', ?, 1)", (_OLD,))


def _seed_recent(conn):
    _seed(
        conn,
        "INSERT INTO recent_downloads (dl_id, title, completed_at) VALUES ('radarr:1', 'T', ?)",
        ("2000-01-01 00:00:00",),
    )


def _poster_row(conn):
    _seed_recent(conn)
    return conn.execute("SELECT * FROM recent_downloads").fetchone()


def _sync_row_gone(conn):
    return _sync_recent_row(conn, _poster_row(conn), {"radarr:1"}, set(), MagicMock())


def _sync_row_poster(conn):
    lookup = MagicMock()
    lookup.poster_for.return_value = "http://x/p.jpg"
    return _sync_recent_row(conn, _poster_row(conn), set(), set(), lookup)


def _no_email_notice(conn):
    insert_settings(
        conn,
        mailgun_domain="test.example.com",
        mailgun_api_key="dummy-key",
        mailgun_from_address="noreply@test.example.com",
    )
    insert_download_notification(conn, email="alice", title="Item", tmdb_id=1)
    check_download_notifications(conn, secret_key="x" * 64)


# (id, prepare, trigger, call, raises)
_CASES = [
    (
        "claim_download_token",
        None,
        _refuse("BEFORE INSERT", "used_download_tokens"),
        lambda c: claim_download_token(c, digest="d", exp=2_000_000_000),
        True,
    ),
    (
        "release_download_token",
        _seed_token,
        _refuse("BEFORE DELETE", "used_download_tokens"),
        lambda c: release_download_token(c, "d"),
        True,
    ),
    (
        "purge_expired_download_tokens",
        _seed_token,
        _refuse("BEFORE DELETE", "used_download_tokens"),
        lambda c: purge_expired_download_tokens(c, now_iso="2999-01-01T00:00:00+00:00"),
        True,
    ),
    (
        "upsert_ratings_cache",
        None,
        _refuse("BEFORE INSERT", "ratings_cache"),
        lambda c: upsert_ratings_cache(c, [(1, "movie", "7.0", None, None, _OLD)]),
        True,
    ),
    (
        "record_success",
        lambda c: record_failure(c, "alice"),
        _refuse("BEFORE DELETE", "login_failures"),
        lambda c: record_success(c, "alice"),
        True,
    ),
    (
        "set_must_change_password",
        lambda c: insert_admin_user(c, username="alice"),
        _refuse("BEFORE UPDATE", "admin_users"),
        lambda c: set_must_change_password(c, "alice", True),
        True,
    ),
    (
        "set_user_email",
        lambda c: insert_admin_user(c, username="alice"),
        _refuse("BEFORE UPDATE", "admin_users"),
        lambda c: set_user_email(c, "alice", "a@example.com"),
        True,
    ),
    (
        "cleanup_recent_downloads",
        _seed_recent,
        _refuse("BEFORE DELETE", "recent_downloads"),
        cleanup_recent_downloads,
        True,
    ),
    (
        "recent_download_that_reappeared",
        None,
        _refuse("BEFORE DELETE", "recent_downloads"),
        _sync_row_gone,
        True,
    ),
    (
        "poster_backfill",
        None,
        _refuse("BEFORE UPDATE", "recent_downloads"),
        _sync_row_poster,
        False,
    ),
    (
        "recent_download_insert",
        None,
        _refuse("BEFORE INSERT", "recent_downloads"),
        lambda c: _batch_insert_completions(c, [("radarr:1", "T", "movie", "")]),
        False,
    ),
    (
        "save_throttle",
        None,
        _refuse("BEFORE INSERT", "arr_search_throttle"),
        lambda c: _save_trigger_to_db(c, "radarr:1", 1_700_000_000.0, 1),
        False,
    ),
    (
        "reconcile_throttle",
        _seed_throttle,
        _refuse("BEFORE DELETE", "arr_search_throttle"),
        reconcile_stranded_throttle,
        False,
    ),
    (
        "clear_throttle",
        _seed_throttle,
        _refuse("BEFORE DELETE", "arr_search_throttle"),
        lambda c: clear_throttle(c, "radarr:1"),
        False,
    ),
    (
        "claim_notifications",
        lambda c: _seed_notification(c, notified=0),
        _refuse("BEFORE UPDATE", "download_notifications"),
        _claim_pending_notifications,
        True,
    ),
    (
        "reconcile_notifications",
        _seed_notification,
        _refuse("BEFORE UPDATE", "download_notifications"),
        reconcile_stranded_notifications,
        True,
    ),
    (
        "release_claim",
        _seed_notification,
        _refuse("BEFORE UPDATE", "download_notifications"),
        lambda c: _release_claim(c, _claimed_notice()),
        False,
    ),
    (
        "release_claims_bulk",
        _seed_notification,
        _refuse("BEFORE UPDATE", "download_notifications"),
        lambda c: _release_claims_bulk(c, [_claimed_notice()]),
        False,
    ),
    (
        "close_notice_without_address",
        None,
        _refuse("BEFORE UPDATE", "download_notifications", "WHEN NEW.notified = 1"),
        _no_email_notice,
        True,
    ),
]


@pytest.mark.parametrize(
    ("prepare", "trigger", "call", "raises"),
    [pytest.param(*case[1:], id=case[0]) for case in _CASES],
)
def test_a_refused_write_leaves_the_connection_free(conn, prepare, trigger, call, raises):
    if prepare is not None:
        prepare(conn)
    conn.commit()
    conn.execute(trigger)

    if raises:
        with pytest.raises(sqlite3.IntegrityError):
            call(conn)
    else:
        call(conn)

    assert not conn.in_transaction
