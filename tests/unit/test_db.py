"""Tests for database schema and operations."""

import sqlite3
import threading

import pytest

from mediaman.db import (
    DB_SCHEMA_VERSION,
    close_db,
    get_db,
    init_db,
    set_connection,
)


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


class TestSchemaV13LegacySessionPurge:
    """C9 / C10: migration v13 hoists the session columns and purges
    legacy rows that pre-date the token-hashing hardening."""

    def test_purges_rows_with_null_token_hash(self, tmp_path):
        """A session row with ``token_hash IS NULL`` is unreachable under
        the new scheme and was also issued under the 7-day hard expiry.
        It must be deleted by the migration — forcing the user to log
        in again so they end up on the hardened path."""
        db_path = tmp_path / "legacy.db"
        # Hand-craft a v12 DB with a legacy session row.
        conn_old = sqlite3.connect(str(db_path))
        conn_old.row_factory = sqlite3.Row
        conn_old.executescript("""
            CREATE TABLE admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE admin_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT INTO admin_users (username, password_hash, created_at)
                VALUES ('legacy', 'x', '2026-01-01');
            INSERT INTO admin_sessions (token, username, created_at, expires_at)
                VALUES ('legacy-raw-token', 'legacy', '2026-01-01', '2099-12-31');
        """)
        conn_old.execute("PRAGMA user_version=12")
        conn_old.commit()
        conn_old.close()

        conn = init_db(str(db_path))
        # Legacy row must have been purged.
        rows = conn.execute("SELECT * FROM admin_sessions").fetchall()
        assert rows == []
        # New columns exist on the table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_sessions)").fetchall()}
        assert {"token_hash", "last_used_at", "fingerprint", "issued_ip"} <= cols
        # Version bumped.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

    def test_purges_rows_with_expiry_over_one_day_cap(self, tmp_path):
        """Any session whose stored expiry is more than the new 1-day
        cap past its created_at gets purged — defensive against
        long-lived rows crafted directly in the DB or left from the old
        7-day hard expiry."""
        db_path = tmp_path / "legacy.db"
        # Start from a fresh v12 DB so we control the state precisely.
        conn = init_db(str(db_path))
        # Drop back to v12 and write a row with a 7-day TTL + populated
        # token_hash (so the null-hash branch doesn't catch it).
        conn.execute("PRAGMA user_version=12")
        conn.execute("DELETE FROM admin_sessions")
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES ('legacy', 'x', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token, token_hash, username, created_at, expires_at) "
            "VALUES ('tok', 'hash', 'legacy', '2026-01-01T00:00:00+00:00', "
            "'2026-01-08T00:00:00+00:00')"  # 7 days > 1 day cap
        )
        conn.commit()
        conn.close()

        # Re-open → migration v13 runs.
        conn = init_db(str(db_path))
        rows = conn.execute("SELECT * FROM admin_sessions").fetchall()
        assert rows == []

    def test_keeps_rows_within_cap(self, tmp_path):
        """A well-formed row inside the 1-day cap must be left alone."""
        db_path = tmp_path / "legacy.db"
        conn = init_db(str(db_path))
        conn.execute("PRAGMA user_version=12")
        conn.execute("DELETE FROM admin_sessions")
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) "
            "VALUES ('keep', 'x', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token, token_hash, username, created_at, expires_at) "
            "VALUES ('tok', 'h', 'keep', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T12:00:00+00:00')"  # 12 h < 1 day cap
        )
        conn.commit()
        conn.close()

        conn = init_db(str(db_path))
        rows = conn.execute("SELECT username FROM admin_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0]["username"] == "keep"

    def test_migration_idempotent(self, db_path):
        """Running init_db twice must not error (migration re-runs are
        guarded)."""
        init_db(str(db_path)).close()
        init_db(str(db_path)).close()  # must not raise

    def test_schema_version_is_current(self, db_path):
        assert DB_SCHEMA_VERSION == 27
        conn = init_db(str(db_path))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 27


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

    def test_migration_from_v13_adds_column(self, tmp_path):
        import sqlite3 as _sq

        db_path = tmp_path / "legacy.db"
        # Bring up a v13 DB first.
        init_db(str(db_path)).close()
        # Now drop the column and reset version to simulate pre-v14.
        # SQLite doesn't easily drop columns, so rebuild the table.
        conn = _sq.connect(str(db_path))
        conn.execute("DROP TABLE scheduled_actions")
        conn.execute("""
            CREATE TABLE scheduled_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_item_id TEXT NOT NULL,
                action TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                execute_at TEXT,
                token TEXT UNIQUE NOT NULL,
                token_used INTEGER NOT NULL DEFAULT 0,
                snoozed_at TEXT,
                snooze_duration TEXT,
                notified INTEGER NOT NULL DEFAULT 0,
                is_reentry INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("PRAGMA user_version=13")
        conn.commit()
        conn.close()

        conn = init_db(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scheduled_actions)").fetchall()}
        assert "delete_status" in cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

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

    def test_migration_from_v14_adds_tables(self, tmp_path):
        import sqlite3 as _sq

        db_path = tmp_path / "legacy.db"
        init_db(str(db_path)).close()
        # Reset to v14.
        conn = _sq.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS scan_runs")
        conn.execute("DROP TABLE IF EXISTS refresh_runs")
        conn.execute("PRAGMA user_version=14")
        conn.commit()
        conn.close()

        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "scan_runs" in tables
        assert "refresh_runs" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

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
        from datetime import datetime, timedelta, timezone

        from mediaman.db import is_scan_running

        conn = init_db(str(db_path))
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
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
        from datetime import datetime, timedelta, timezone

        from mediaman.db import heartbeat_scan_run, is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        assert run_id is not None

        # Pretend three hours have passed, but the worker is still
        # heartbeating — the row must not be considered stale.
        conn.execute(
            "UPDATE scan_runs SET started_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
                run_id,
            ),
        )
        conn.commit()
        heartbeat_scan_run(conn, run_id)
        assert is_scan_running(conn) is True

    def test_lapsed_heartbeat_unblocks_new_run(self, db_path):
        """A run whose heartbeat is older than the stale window is ignored."""
        from datetime import datetime, timedelta, timezone

        from mediaman.db import is_scan_running, start_scan_run

        conn = init_db(str(db_path))
        run_id = start_scan_run(conn)
        # Force the heartbeat back into the past beyond the stale window.
        long_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
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
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO keep_tokens_used (token_hash, used_at) VALUES ('abc', ?)", (now,))
        cursor = conn.execute(
            "INSERT OR IGNORE INTO keep_tokens_used (token_hash, used_at) VALUES ('abc', ?)", (now,)
        )
        assert cursor.rowcount == 0, "Duplicate insert must be ignored"

    def test_migration_from_v17_adds_table(self, tmp_path):
        import sqlite3 as _sq

        db_path = tmp_path / "legacy.db"
        init_db(str(db_path)).close()
        # Simulate a v17 DB (no keep_tokens_used yet).
        conn = _sq.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS keep_tokens_used")
        conn.execute("PRAGMA user_version=17")
        conn.commit()
        conn.close()

        conn = init_db(str(db_path))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "keep_tokens_used" in tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

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
