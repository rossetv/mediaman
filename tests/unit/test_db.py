"""Tests for database schema and operations."""

import sqlite3
import threading
from datetime import UTC

import pytest

from mediaman.db import (
    CUTOVER_VERSION,
    DB_SCHEMA_VERSION,
    SchemaFromFutureError,
    SchemaTooOldError,
    close_db,
    get_db,
    init_db,
    set_connection,
)
from mediaman.db.migrations import apply_migrations


class TestInitDb:
    def test_creates_tables(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        assert "admin_sessions" in tables
        assert "admin_users" in tables
        assert "audit_log" in tables
        assert "media_items" in tables
        assert "scheduled_actions" in tables
        assert "settings" in tables
        assert "subscribers" in tables

    def test_creates_schema_version(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA user_version")
        version = cursor.fetchone()[0]
        assert version == DB_SCHEMA_VERSION

    def test_idempotent_init(self, db_path):
        conn1 = init_db(str(db_path))
        conn1.close()
        conn2 = init_db(str(db_path))
        cursor = conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        assert "media_items" in tables

    def test_wal_mode_enabled(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db_path):
        conn = init_db(str(db_path))
        cursor = conn.execute("PRAGMA foreign_keys")
        enabled = cursor.fetchone()[0]
        assert enabled == 1


class TestSchemaV3:
    def test_media_items_has_show_rating_key(self, db_path):
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()]
        assert "show_rating_key" in cols
        conn.close()

    def test_kept_shows_table_exists(self, db_path):
        conn = init_db(str(db_path))
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        assert "kept_shows" in tables
        conn.close()

    def test_kept_shows_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(kept_shows)").fetchall()]
        assert "show_rating_key" in cols
        assert "show_title" in cols
        assert "action" in cols
        assert "execute_at" in cols
        conn.close()


class TestThreadLocalConnection:
    """Regression: ``get_db()`` must return a per-thread connection.

    Previously a single module-global connection was shared across the
    scanner, scheduler, and web threads. ``conn.commit()`` on one
    thread could then commit another thread's pending writes, and
    long transactions could block unrelated work. These tests confirm
    each thread now gets an isolated connection pointed at the same
    DB file.
    """

    def test_separate_connections_per_thread(self, db_path):
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        observed: dict[str, sqlite3.Connection | None] = {"worker": None}

        def worker() -> None:
            observed["worker"] = get_db()
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert observed["worker"] is not None
        # The worker thread must not receive the main-thread connection.
        assert observed["worker"] is not conn_main

    def test_each_thread_sees_same_schema(self, db_path):
        """Both threads point at the same DB file — schema is shared."""
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        # Write something in the main thread.
        conn_main.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) "
            "VALUES ('thread_test', 'ok', 0, '2026-01-01')"
        )
        conn_main.commit()

        found: dict[str, str | None] = {"value": None}

        def worker() -> None:
            conn = get_db()
            row = conn.execute("SELECT value FROM settings WHERE key='thread_test'").fetchone()
            found["value"] = row["value"] if row else None
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert found["value"] == "ok"

    def test_close_db_only_touches_thread_local(self, db_path):
        """close_db() must not close the bootstrap connection."""
        conn_main = init_db(str(db_path))
        set_connection(conn_main)

        def worker() -> None:
            get_db()  # lazily open this thread's conn
            close_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        # The main-thread connection must still be usable.
        row = conn_main.execute("SELECT 1").fetchone()
        assert row[0] == 1


class TestSchemaV13SessionColumns:
    """v13 columns exist on fresh DB; legacy-row purge logic was in the migration
    (now squashed). These tests confirm the schema shape on fresh installations."""

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not error."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()  # must not raise

    def test_schema_version_is_current(self, db_path):
        assert DB_SCHEMA_VERSION == 35
        conn = init_db(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 35

    def test_admin_sessions_has_security_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_sessions)").fetchall()}
        assert {"token_hash", "last_used_at", "fingerprint", "issued_ip"} <= cols
        conn.close()

    def test_admin_users_has_must_change_password(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_users)").fetchall()}
        assert "must_change_password" in cols
        conn.close()


class TestSchemaV14DeleteStatus:
    """v14 adds ``scheduled_actions.delete_status`` for the two-phase delete."""

    def test_column_present_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scheduled_actions)").fetchall()}
        assert "delete_status" in cols

    def test_default_is_pending(self, db_path):
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m1', 't', 'movie', 1, 'm1', '2026-01-01', '/tmp/x', 1)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token) "
            "VALUES ('m1', 'scheduled_deletion', '2026-01-01', 'tok1')"
        )
        status = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE token='tok1'"
        ).fetchone()[0]
        assert status == "pending"

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not raise on the v14 step."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()


class TestSchemaV15JobRunTables:
    """v15 adds ``scan_runs`` and ``refresh_runs`` for DB-backed job state."""

    def test_tables_present_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "scan_runs" in tables
        assert "refresh_runs" in tables

    def test_scan_runs_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
        assert cols >= {"id", "started_at", "finished_at", "status", "error"}

    def test_refresh_runs_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(refresh_runs)").fetchall()}
        assert cols >= {"id", "started_at", "finished_at", "status", "error"}

    def test_migration_idempotent(self, db_path):
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()


class TestJobRunHelpers:
    """Tests for the scan_runs / refresh_runs DB helper functions."""

    def test_is_scan_running_false_when_empty(self, db_path):
        from mediaman.db import is_scan_running

        conn = init_db(str(db_path))
        assert is_scan_running(conn) is False

    def test_start_scan_run_returns_id(self, db_path):
        from mediaman.db import start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        assert run_id is not None
        assert isinstance(run_id, int)

    def test_is_scan_running_true_after_start(self, db_path):
        from mediaman.db import is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        start_scan_run(conn)
        assert is_scan_running(conn) is True

    def test_start_scan_run_returns_none_when_already_running(self, db_path):
        from mediaman.db import start_scan_run

        conn = init_db(str(db_path))
        run_id_1 = start_scan_run(conn)
        assert run_id_1 is not None
        run_id_2 = start_scan_run(conn)
        assert run_id_2 is None

    def test_finish_scan_run_releases_lock(self, db_path):
        from mediaman.db import finish_scan_run, is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        assert is_scan_running(conn) is True
        finish_scan_run(conn, run_id, "done")
        assert is_scan_running(conn) is False

    def test_finish_scan_run_on_error_still_releases_lock(self, db_path):
        """A crashed run (finished with 'error') must release the lock."""
        from mediaman.db import finish_scan_run, is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        finish_scan_run(conn, run_id, "error", "something went wrong")
        assert is_scan_running(conn) is False

    def test_stale_scan_run_does_not_block(self, db_path):
        """A row whose heartbeat lapsed beyond the stale window is treated as crashed."""
        from datetime import datetime, timedelta

        from mediaman.db import is_scan_running

        conn = init_db(str(db_path))
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        conn.execute(
            "INSERT INTO scan_runs (started_at, status) VALUES (?, 'running')",
            (stale,),
        )
        conn.commit()
        assert is_scan_running(conn) is False

    def test_heartbeat_keeps_long_run_alive(self, db_path):
        """A long scan that renews its heartbeat must stay marked running.

        Regression for finding 9: previously the fixed two-hour cutoff
        meant a slow scan would silently appear "stale" while still
        running, allowing a sibling worker to start an overlapping job.
        With the heartbeat lease the live run keeps blocking new starts
        as long as it ticks within the stale window.
        """
        from datetime import datetime, timedelta

        from mediaman.db import heartbeat_scan_run, is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        assert run_id is not None

        # Pretend three hours have passed, but the worker is still
        # heartbeating — the row must not be considered stale.
        conn.execute(
            "UPDATE scan_runs SET started_at = ? WHERE id = ?",
            (
                (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
                run_id,
            ),
        )
        conn.commit()
        heartbeat_scan_run(conn, run_id)
        assert is_scan_running(conn) is True

    def test_lapsed_heartbeat_unblocks_new_run(self, db_path):
        """A run whose heartbeat is older than the stale window is ignored."""
        from datetime import datetime, timedelta

        from mediaman.db import is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        # Force the heartbeat back into the past beyond the stale window.
        long_ago = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        conn.execute(
            "UPDATE scan_runs SET heartbeat_at = ?, started_at = ? WHERE id = ?",
            (long_ago, long_ago, run_id),
        )
        conn.commit()
        assert is_scan_running(conn) is False
        # And a new run can start.
        new_id = start_scan_run(conn)
        assert new_id is not None
        assert new_id != run_id

    def test_owner_id_is_recorded(self, db_path):
        """Every new run row carries a non-empty owner_id."""
        from mediaman.db import start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        row = conn.execute("SELECT owner_id FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["owner_id"]

    def test_is_refresh_running_false_when_empty(self, db_path):
        from mediaman.db import is_refresh_running

        conn = init_db(str(db_path))
        assert is_refresh_running(conn) is False

    def test_start_and_finish_refresh_run(self, db_path):
        from mediaman.db import finish_refresh_run, is_refresh_running, start_refresh_run

        conn = init_db(str(db_path))
        run_id = start_refresh_run(conn)
        assert run_id is not None
        assert is_refresh_running(conn) is True
        finish_refresh_run(conn, run_id, "done")
        assert is_refresh_running(conn) is False

    def test_second_refresh_run_blocked(self, db_path):
        from mediaman.db import start_refresh_run

        conn = init_db(str(db_path))
        assert start_refresh_run(conn) is not None
        assert start_refresh_run(conn) is None


class TestSchemaV18KeepTokensUsed:
    """v18 adds ``keep_tokens_used`` for HMAC token replay prevention (H27)."""

    def test_table_present_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "keep_tokens_used" in tables

    def test_table_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(keep_tokens_used)").fetchall()}
        assert cols >= {"token_hash", "used_at"}

    def test_token_hash_is_primary_key(self, db_path):
        """INSERT OR IGNORE on a duplicate token_hash must silently succeed (rowcount 0)."""
        conn = init_db(str(db_path))
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO keep_tokens_used (token_hash, used_at) VALUES ('abc', ?)", (now,))
        cursor = conn.execute(
            "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES ('abc', ?)", (now,)
        )
        assert cursor.rowcount == 0, "Duplicate insert must be ignored"

    def test_migration_idempotent(self, db_path):
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()  # must not raise


class TestSchemaV25UniqueActiveDeletion:
    """v25 adds a partial unique index preventing two active pending
    deletions for the same media_item_id (finding 9)."""

    def test_index_present(self, db_path):
        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_scheduled_actions_unique_active_deletion'"
        ).fetchall()
        assert len(rows) == 1

    def test_duplicate_active_deletion_blocked(self, db_path):
        """Inserting a second active pending deletion for the same item raises."""
        import sqlite3 as _sq

        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m1', 't', 'movie', 1, 'm1', '2026-01-01', '/tmp/x', 1)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m1', 'scheduled_deletion', '2026-01-01', 'tok-a', 0, 'pending')"
        )
        with pytest.raises(_sq.IntegrityError):
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
                "VALUES ('m1', 'scheduled_deletion', '2026-01-02', 'tok-b', 0, 'pending')"
            )

    def test_consumed_row_does_not_block_new_one(self, db_path):
        """Once token_used=1 the slot frees up — the user might re-enter scope."""
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m2', 't', 'movie', 1, 'm2', '2026-01-01', '/tmp/x', 1)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m2', 'scheduled_deletion', '2026-01-01', 'tok-c', 1, 'pending')"
        )
        # token_used=1 — second row must be allowed.
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m2', 'scheduled_deletion', '2026-01-02', 'tok-d', 0, 'pending')"
        )
        conn.commit()

    def test_deleted_status_does_not_block_new_one(self, db_path):
        """A row past the pending stage shouldn't block a fresh deletion."""
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO media_items (id, title, media_type, plex_library_id, "
            "plex_rating_key, added_at, file_path, file_size_bytes) "
            "VALUES ('m3', 't', 'movie', 1, 'm3', '2026-01-01', '/tmp/x', 1)"
        )
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m3', 'scheduled_deletion', '2026-01-01', 'tok-e', 0, 'deleting')"
        )
        # delete_status='deleting' — second pending row must be allowed.
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(media_item_id, action, scheduled_at, token, token_used, delete_status) "
            "VALUES ('m3', 'scheduled_deletion', '2026-01-02', 'tok-f', 0, 'pending')"
        )
        conn.commit()


class TestSchemaV23UsedDownloadTokens:
    """v23 adds the persistent download-token store (finding 2)."""

    def test_table_present(self, db_path):
        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "used_download_tokens" in tables

    def test_token_hash_is_unique(self, db_path):
        import sqlite3 as _sq

        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO used_download_tokens (token_hash, expires_at, used_at) "
            "VALUES ('abc', '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(_sq.IntegrityError):
            conn.execute(
                "INSERT INTO used_download_tokens (token_hash, expires_at, used_at) "
                "VALUES ('abc', '2026-01-02', '2026-01-02')"
            )


class TestSchemaV24JobHeartbeatColumns:
    """v24 adds owner_id + heartbeat_at to scan_runs and refresh_runs (finding 9)."""

    def test_scan_runs_has_heartbeat_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
        assert "owner_id" in cols
        assert "heartbeat_at" in cols

    def test_refresh_runs_has_heartbeat_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(refresh_runs)").fetchall()}
        assert "owner_id" in cols
        assert "heartbeat_at" in cols


class TestSchemaV26NewsletterDeliveries:
    """v26 adds per-recipient newsletter delivery tracking (finding 23)."""

    def test_table_present(self, db_path):
        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "newsletter_deliveries" in tables

    def test_columns(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(newsletter_deliveries)").fetchall()}
        assert cols >= {"scheduled_action_id", "recipient", "sent_at", "error", "attempted_at"}

    def test_primary_key_prevents_duplicate_records(self, db_path):
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO newsletter_deliveries "
            "(scheduled_action_id, recipient, sent_at, error, attempted_at) "
            "VALUES (1, 'a@x', '2026-01-01', NULL, '2026-01-01')"
        )
        # INSERT OR REPLACE must overwrite without raising.
        conn.execute(
            "INSERT OR REPLACE INTO newsletter_deliveries "
            "(scheduled_action_id, recipient, sent_at, error, attempted_at) "
            "VALUES (1, 'a@x', '2026-01-02', NULL, '2026-01-02')"
        )
        rows = conn.execute(
            "SELECT sent_at FROM newsletter_deliveries WHERE scheduled_action_id=1"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["sent_at"] == "2026-01-02"


class TestSchemaV31AuditActor:
    """v31 adds an ``actor`` column to ``audit_log`` (Domain 05 HIGH)."""

    def test_audit_log_has_actor_column_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        assert "actor" in cols
        conn.close()

    def test_actor_column_is_nullable(self, db_path):
        """Existing call sites (which do not yet pass actor) must still work."""
        conn = init_db(str(db_path))
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
            "VALUES ('m1', 'scheduled', 'auto', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        row = conn.execute("SELECT actor FROM audit_log").fetchone()
        assert row["actor"] is None
        conn.close()

    def test_actor_index_present(self, db_path):
        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_audit_log_actor",),
        ).fetchall()
        assert len(rows) == 1
        conn.close()

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not raise on the v31 step."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()


class TestSchemaV32HotPathIndexes:
    """v32 adds two indexes for previously full-scan WHERE clauses (Domain 05)."""

    def test_media_items_index_present_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_media_items_plex_library_id",),
        ).fetchall()
        assert len(rows) == 1
        conn.close()

    def test_audit_log_action_index_present_on_fresh_db(self, db_path):
        conn = init_db(str(db_path))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_audit_log_action",),
        ).fetchall()
        assert len(rows) == 1
        conn.close()

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not raise on the v32 step."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()


class TestSchemaV33AuditLogTamperEvidence:
    """v33 adds BEFORE-UPDATE / BEFORE-DELETE triggers on ``audit_log``.

    Tamper-evidence rather than tamper-prevention: anyone with DB-file
    write access can drop the trigger first, but doing so leaves a
    visible diff in ``sqlite_master`` so an operator running ``.schema``
    notices.
    """

    def test_update_on_existing_row_is_refused(self, db_path):
        conn = init_db(str(db_path))
        try:
            conn.execute(
                "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
                "VALUES ('test', 'fixture', '{}', '2026-05-02T00:00:00Z')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE audit_log SET detail='tampered' WHERE media_item_id='test'")
        finally:
            conn.close()

    def test_delete_on_existing_row_is_refused(self, db_path):
        conn = init_db(str(db_path))
        try:
            conn.execute(
                "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
                "VALUES ('test', 'fixture', '{}', '2026-05-02T00:00:00Z')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM audit_log WHERE media_item_id='test'")
        finally:
            conn.close()

    def test_insert_remains_unrestricted(self, db_path):
        """The application must still be able to write new audit rows."""
        conn = init_db(str(db_path))
        try:
            conn.execute(
                "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
                "VALUES ('a', 'one', '{}', '2026-05-02T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
                "VALUES ('b', 'two', '{}', '2026-05-02T00:00:01Z')"
            )
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_triggers_present_in_sqlite_master(self, db_path):
        """The triggers must be visible to operator inspection."""
        conn = init_db(str(db_path))
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            assert "audit_log_no_update" in names
            assert "audit_log_no_delete" in names
        finally:
            conn.close()


class TestFactories:
    """Proof-of-life for ``tests/helpers/factories.py``.

    Verifies that the factory helpers produce dicts whose keys match the
    ``media_items`` and ``scheduled_actions`` schema so they can be used
    as a quick scaffold in other tests rather than duplicating raw SQL
    dictionaries everywhere.
    """

    def test_make_media_item_roundtrip(self, db_path):
        """A dict from ``make_media_item`` can be inserted into the DB."""
        from tests.helpers.factories import make_media_item

        conn = init_db(str(db_path))
        item = make_media_item(id="f1", title="Factory Film", media_type="movie")
        conn.execute(
            "INSERT INTO media_items "
            "(id, title, media_type, plex_library_id, plex_rating_key, "
            "added_at, file_path, file_size_bytes) "
            "VALUES (:id, :title, :media_type, :plex_library_id, "
            ":plex_rating_key, :added_at, :file_path, :file_size_bytes)",
            item,
        )
        conn.commit()
        row = conn.execute("SELECT title, media_type FROM media_items WHERE id='f1'").fetchone()
        assert row["title"] == "Factory Film"
        assert row["media_type"] == "movie"
        conn.close()

    def test_make_media_item_defaults_are_sane(self):
        """Default values do not require any keyword arguments."""
        from tests.helpers.factories import make_media_item

        item = make_media_item()
        assert item["media_type"] == "movie"
        assert item["file_size_bytes"] > 0
        # added_at must be an ISO 8601 string (so it can be stored as TEXT).
        assert "T" in item["added_at"] or "-" in item["added_at"]

    def test_make_scheduled_action_defaults_are_sane(self):
        """Scheduled action factory produces a complete dict."""
        from tests.helpers.factories import make_scheduled_action

        action = make_scheduled_action()
        assert action["action"] == "scheduled_deletion"
        assert action["token_used"] is False
        assert action["notified"] is False
        # execute_at must be after scheduled_at.
        assert action["execute_at"] > action["scheduled_at"]


class TestMigrationSquash:
    """Verifies the squashed-baseline behaviour introduced on 2026-05-04.

    Fresh DBs go straight to CUTOVER_VERSION. Pre-cutover DBs raise
    SchemaTooOldError so operators receive a clear upgrade instruction
    rather than corrupted state.
    """

    def test_fresh_db_lands_at_current_schema_version(self, tmp_path):
        """A brand-new database must be stamped at DB_SCHEMA_VERSION.

        ``_SCHEMA`` already reflects every post-cutover migration's
        resulting shape, so a fresh DB skips the registry walk and is
        stamped at the latest version directly.
        """
        conn = sqlite3.connect(str(tmp_path / "fresh.db"))
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == DB_SCHEMA_VERSION
        conn.close()

    def test_db_at_version_10_raises_schema_too_old(self, tmp_path):
        """A database at version 10 (below the cutover) must raise SchemaTooOldError."""
        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version=10")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        with pytest.raises(SchemaTooOldError) as exc_info:
            apply_migrations(conn)
        conn.close()

        msg = str(exc_info.value)
        assert "version 10" in msg
        assert str(CUTOVER_VERSION) in msg
        assert "1.8.x" in msg  # the transit release name

    def test_error_message_is_actionable(self, tmp_path):
        """The error message must name the current version, cutover, and the
        release the user must transit through."""
        conn = sqlite3.connect(str(tmp_path / "old2.db"))
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "old2.db"))
        conn.row_factory = sqlite3.Row
        with pytest.raises(SchemaTooOldError, match="1.8.x"):
            apply_migrations(conn)
        conn.close()

    def test_db_at_cutover_advances_to_current(self, tmp_path):
        """A DB at exactly CUTOVER_VERSION walks the post-cutover registry.

        The user's existing v34 deployment lands here on first boot of the
        post-squash release: v35 is a no-op DDL marker that bumps user_version
        and lets bootstrap_crypto's migrate_legacy_ciphertexts run.
        """
        db_path = str(tmp_path / "cutover.db")
        conn = sqlite3.connect(db_path)
        # Lay down the baseline schema so the connection has the tables
        # the post-cutover migrations may need.
        from mediaman.db.schema_definition import _SCHEMA

        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={CUTOVER_VERSION}")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == DB_SCHEMA_VERSION
        conn.close()

    def test_db_at_current_schema_version_is_noop(self, tmp_path):
        """A database already at DB_SCHEMA_VERSION must not be modified."""
        conn = sqlite3.connect(str(tmp_path / "current.db"))
        conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "current.db"))
        conn.row_factory = sqlite3.Row
        # Must not raise; user_version stays the same.
        apply_migrations(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == DB_SCHEMA_VERSION
        conn.close()

    def test_db_from_future_raises(self, tmp_path):
        """A database stamped above DB_SCHEMA_VERSION must refuse to start.

        The most likely cause is a downgrade or a backup restored from a
        newer release; silently accepting it would let the app come up
        "healthy" and then corrupt data on the first write to a column the
        old code does not know about.
        """
        future_version = DB_SCHEMA_VERSION + 1
        conn = sqlite3.connect(str(tmp_path / "future.db"))
        conn.execute(f"PRAGMA user_version={future_version}")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "future.db"))
        conn.row_factory = sqlite3.Row
        with pytest.raises(SchemaFromFutureError) as exc_info:
            apply_migrations(conn)
        conn.close()

        msg = str(exc_info.value)
        assert str(future_version) in msg
        assert str(DB_SCHEMA_VERSION) in msg
