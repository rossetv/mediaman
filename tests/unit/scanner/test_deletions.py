"""Tests for mediaman.scanner.deletions.

Covers: DeletionExecutor.execute (dry_run, no allowed roots, actual delete
path, stuck-state recovery) and recover_stuck_deletions.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner.deletions import DeletionExecutor, recover_stuck_deletions
from tests.helpers.factories import (
    insert_kept_show,
    insert_media_item,
    insert_scheduled_action,
)


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _track(conn, media_id, *paths):
    """Record *paths* as the item's tracked files, sized as they are on disk."""
    files = [
        {"path": str(p), "size": os.path.getsize(p) if os.path.lexists(p) else 0} for p in paths
    ]
    conn.execute(
        "UPDATE media_items SET tracked_files = ? WHERE id = ?", (json.dumps(files), media_id)
    )
    conn.commit()


def _insert_media(conn, *, media_id="m1", title="Test Film", file_path="/tmp/test.mkv"):
    """Insert a movie whose one tracked file is *file_path* (none for a blank path)."""
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        plex_rating_key=media_id,
        file_path=file_path,
        file_size_bytes=1000000,
    )
    if file_path.strip():
        _track(conn, media_id, file_path)


def _insert_pending_deletion(conn, *, media_id="m1", execute_at=None):
    """Insert a scheduled_deletion row with execute_at in the past."""
    if execute_at is None:
        execute_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        token=f"tok-{media_id}",
        execute_at=execute_at,
        token_used=False,
        delete_status="pending",
    )


class TestDryRun:
    def test_dry_run_does_not_delete_file(self, conn, tmp_path):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        executor = DeletionExecutor(conn=conn, dry_run=True)
        result = executor.execute()

        assert result["deleted"] == 0
        assert real_file.exists()

    def test_dry_run_writes_audit_entry(self, conn, tmp_path):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        DeletionExecutor(conn=conn, dry_run=True).execute()

        row = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert row is not None
        assert row["action"] == "dry_run_skip"

    @pytest.mark.parametrize("keep", ["forever", "show"])
    def test_dry_run_leaves_a_kept_item_out_of_the_preview_and_cancels_nothing(
        self, conn, tmp_path, keep
    ):
        insert_media_item(conn, id="kept", title="Kept", file_path=str(tmp_path / "k.mkv"))
        insert_media_item(conn, id="due", title="Due", file_path=str(tmp_path / "d.mkv"))
        _insert_pending_deletion(conn, media_id="kept")
        _insert_pending_deletion(conn, media_id="due")
        if keep == "show":
            conn.execute("UPDATE media_items SET show_rating_key = 'show1' WHERE id = 'kept'")
            insert_kept_show(conn, show_rating_key="show1", action="protected_forever")
        else:
            insert_scheduled_action(
                conn, media_item_id="kept", action="protected_forever", token="tok-keep"
            )
        conn.commit()

        DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False).execute()

        previews = conn.execute(
            "SELECT media_item_id FROM audit_log WHERE action = 'dry_run_skip'"
        ).fetchall()
        assert [r["media_item_id"] for r in previews] == ["due"]
        assert conn.execute(
            "SELECT 1 FROM scheduled_actions WHERE media_item_id = 'kept' "
            "AND action = 'scheduled_deletion' AND delete_status = 'pending'"
        ).fetchone()


class TestNoAllowedRoots:
    def test_skips_deletion_when_roots_not_configured(self, conn, tmp_path, monkeypatch):
        # Ensure no roots in DB or env.
        conn.execute("DELETE FROM settings WHERE key='delete_allowed_roots'")
        conn.commit()
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)

        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        assert real_file.exists()


class TestSuccessfulDeletion:
    def test_deletes_file_and_returns_count(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 1
        assert not real_file.exists()

    def test_reclaimed_bytes_summed(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"x" * 1_000)

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        # The bytes that left the disk, not the 1000000 stored on the row.
        assert result["reclaimed_bytes"] == 1_000

    def test_audit_log_entry_written(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn)

        DeletionExecutor(conn=conn, dry_run=False).execute()

        row = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert row is not None
        assert row["action"] == "deleted"

    def test_calls_radarr_unmonitor(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "film.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        # Give the media item a radarr_id.
        insert_media_item(
            conn,
            id="r1",
            title="Radarr Film",
            plex_rating_key="r1",
            file_path=str(real_file),
            file_size_bytes=100,
            radarr_id=42,
        )
        _track(conn, "r1", real_file)
        insert_scheduled_action(
            conn,
            media_item_id="r1",
            action="scheduled_deletion",
            token="tok-r1",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="pending",
        )

        fake_radarr = MagicMock()
        DeletionExecutor(conn=conn, dry_run=False, radarr_client=fake_radarr).execute()

        fake_radarr.unmonitor_movie.assert_called_once_with(42)

    def test_future_deletions_are_not_executed(self, conn, tmp_path, monkeypatch):
        real_file = tmp_path / "future.mkv"
        real_file.write_bytes(b"data")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))

        future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        _insert_media(conn, file_path=str(real_file))
        _insert_pending_deletion(conn, execute_at=future)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        assert real_file.exists()


class TestRecoverStuckDeletions:
    def test_resets_to_pending_when_file_present(self, conn, tmp_path):
        real_file = tmp_path / "stuck.mkv"
        real_file.write_bytes(b"data")

        _insert_media(conn, file_path=str(real_file))
        conn.execute(
            "UPDATE scheduled_actions SET delete_status='deleting' WHERE media_item_id='m1'"
        )
        conn.commit()

        # Need a row — insert one manually in deleting state.
        _insert_media(conn, media_id="m2", file_path=str(real_file))
        insert_scheduled_action(
            conn,
            media_item_id="m2",
            action="scheduled_deletion",
            token="tok-m2",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m2'"
        ).fetchone()
        assert row["delete_status"] == "pending"

    def test_completes_cleanup_when_file_gone(self, conn, tmp_path, monkeypatch):
        # File does NOT exist but its path is within the configured delete
        # roots — should log as already-deleted and remove the row.
        root = tmp_path / "library"
        root.mkdir()
        (root / "other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        missing_path = str(root / "already_gone.mkv")

        _insert_media(conn, media_id="m3", file_path=missing_path)
        insert_scheduled_action(
            conn,
            media_item_id="m3",
            action="scheduled_deletion",
            token="tok-m3",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        row = conn.execute("SELECT id FROM scheduled_actions WHERE media_item_id='m3'").fetchone()
        assert row is None  # Row removed — cleanup complete.

    @pytest.mark.parametrize("root_state", ["missing", "empty"])
    def test_file_gone_with_root_unavailable_is_not_booked_deleted(
        self, conn, tmp_path, monkeypatch, root_state
    ):
        root = tmp_path / "library"
        if root_state == "empty":
            root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        _insert_media(conn, media_id="m3", file_path=str(root / "M" / "M.mkv"))
        insert_scheduled_action(
            conn,
            media_item_id="m3",
            action="scheduled_deletion",
            token="tok-m3",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m3'"
        ).fetchone()
        assert row["delete_status"] == "pending"
        assert (
            conn.execute(
                "SELECT 1 FROM audit_log WHERE media_item_id='m3' AND action='deleted'"
            ).fetchone()
            is None
        )

    def test_no_stuck_rows_is_noop(self, conn):
        # Must not raise when there are no deleting rows.
        recover_stuck_deletions(conn)

    def test_file_gone_outside_roots_does_not_fabricate_audit(self, conn, tmp_path, monkeypatch):
        """A 'deleting' row whose file is absent but whose stored path is
        OUTSIDE the configured delete roots must NOT be completed as a
        deletion — mediaman could never have removed it, so writing a
        'deleted N bytes' audit row would corrupt the audit trail and the
        reclaimed-bytes stats. The row is reset to 'pending' and no audit
        entry is created.
        """
        root = tmp_path / "library"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        # Path is absent AND outside the configured root.
        outside_path = str(tmp_path / "elsewhere" / "ghost.mkv")

        _insert_media(conn, media_id="m4", file_path=outside_path)
        insert_scheduled_action(
            conn,
            media_item_id="m4",
            action="scheduled_deletion",
            token="tok-m4",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        # Row preserved and reset to pending for operator review.
        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m4'"
        ).fetchone()
        assert row is not None
        assert row["delete_status"] == "pending"
        # No phantom 'deleted' audit entry was fabricated.
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id='m4' AND action='deleted'"
        ).fetchone()
        assert audit is None

    def test_file_gone_with_empty_path_does_not_fabricate_audit(self, conn, monkeypatch):
        """An empty stored path is never within any root, so the
        file-absent branch must not complete a deletion off it either.
        """
        monkeypatch.delenv("MEDIAMAN_DELETE_ROOTS", raising=False)
        _insert_media(conn, media_id="m5", file_path="")
        insert_scheduled_action(
            conn,
            media_item_id="m5",
            action="scheduled_deletion",
            token="tok-m5",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m5'"
        ).fetchone()
        assert row is not None
        assert row["delete_status"] == "pending"
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id='m5' AND action='deleted'"
        ).fetchone()
        assert audit is None


class TestPlexCraftedRootPath:
    """A buggy or compromised Plex response can supply a ``part.file`` that
    happens to equal a configured delete root. The cleanup loop must refuse
    that target, leave the row recoverable for a later run, and never wipe
    the mount.
    """

    def test_part_file_equal_to_root_is_refused(self, conn, tmp_path, monkeypatch):
        """A media row whose ``file_path`` equals the configured root
        must NOT be deleted — strict-descendant rule blocks it.
        """
        # Configure a root and populate it with files we expect to keep.
        root = tmp_path / "library"
        root.mkdir()
        (root / "movie1.mkv").write_text("keep")
        (root / "movie2.mkv").write_text("keep")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))

        # Crafted DB row: file_path is the root itself (mimics a Plex
        # response with part.file == "/media").
        _insert_media(conn, file_path=str(root))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        # Nothing was deleted, nothing was lost.
        assert result["deleted"] == 0
        assert root.exists()
        assert (root / "movie1.mkv").exists()
        assert (root / "movie2.mkv").exists()
        # Row is reset to pending so the next run can re-evaluate.
        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchone()
        assert row["delete_status"] == "pending"

    def test_part_file_outside_root_is_refused(self, conn, tmp_path, monkeypatch):
        """A media row whose ``file_path`` escapes the allowlist is refused."""
        root = tmp_path / "library"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "important.txt").write_text("preserve")

        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))

        _insert_media(conn, file_path=str(outside / "important.txt"))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        assert (outside / "important.txt").exists()


class TestEmptyFilePathParking:
    def test_empty_path_is_parked_with_audit_and_does_not_livelock(
        self, conn, tmp_path, monkeypatch
    ):
        """A pending deletion whose ``file_path`` is empty must not cycle
        forever between 'deleting' and 'pending'.

        ``delete_path("")`` always refuses and recovery always resets it to
        pending, so without intervention the row livelocks invisibly. The
        executor must park it: drop the scheduled action and write a
        permanent-failure audit the operator can see.
        """
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        _insert_media(conn, media_id="empty1", file_path="")
        _insert_pending_deletion(conn, media_id="empty1")

        result = DeletionExecutor(conn=conn, dry_run=False).execute()

        assert result["deleted"] == 0
        # Action parked — no scheduled_actions row remains to cycle.
        remaining = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='empty1'"
        ).fetchone()
        assert remaining is None
        # Operator-visible permanent-failure audit written.
        audit = conn.execute("SELECT action FROM audit_log WHERE media_item_id='empty1'").fetchone()
        assert audit is not None
        assert audit["action"] == "delete_failed_permanent"

    def test_whitespace_only_path_is_parked(self, conn, tmp_path, monkeypatch):
        """A path that is only whitespace is as undeletable as an empty one
        (``delete_path`` refuses it) and must be parked, not cycled.
        """
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(tmp_path))
        _insert_media(conn, media_id="ws1", file_path="   ")
        _insert_pending_deletion(conn, media_id="ws1")

        DeletionExecutor(conn=conn, dry_run=False).execute()

        remaining = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='ws1'"
        ).fetchone()
        assert remaining is None
        audit = conn.execute("SELECT action FROM audit_log WHERE media_item_id='ws1'").fetchone()
        assert audit is not None
        assert audit["action"] == "delete_failed_permanent"


class TestDryRunDoesNotRecover:
    def test_dry_run_does_not_mutate_stuck_deletions(self, conn, tmp_path, monkeypatch):
        """A dry-run preview must mutate nothing — including not running
        stuck-deletion recovery, which commits real state changes and audit
        rows.

        A row left in the 'deleting' state must be untouched after a dry-run
        execute; only a live pass may reconcile it.
        """
        root = tmp_path / "library"
        root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        # File absent but within roots — a LIVE recovery would complete the
        # cleanup (drop the row + write a 'deleted' audit). Dry-run must not.
        missing = str(root / "gone.mkv")
        _insert_media(conn, media_id="stuck1", file_path=missing)
        insert_scheduled_action(
            conn,
            media_item_id="stuck1",
            action="scheduled_deletion",
            token="tok-stuck1",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        DeletionExecutor(conn=conn, dry_run=True, cleanup_snoozes=False).execute()

        # Row untouched — still 'deleting', not reconciled.
        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='stuck1'"
        ).fetchone()
        assert row is not None
        assert row["delete_status"] == "deleting"
        # No 'deleted' audit fabricated by a recovery that should not have run.
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE media_item_id='stuck1' AND action='deleted'"
        ).fetchone()
        assert audit is None


def _due_row(conn, media_id):
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="scheduled_deletion",
        token=f"tok-{media_id}",
        execute_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        token_used=False,
    )


def _actions(conn, media_id):
    return [
        r["action"]
        for r in conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id=?", (media_id,)
        )
    ]


def _audits(conn, media_id):
    return [
        r["action"]
        for r in conn.execute("SELECT action FROM audit_log WHERE media_item_id=?", (media_id,))
    ]


def _keep_forever(other, media_id):
    from mediaman.web.repository.library_api import apply_keep_in_tx

    apply_keep_in_tx(
        other,
        media_id=media_id,
        action="protected_forever",
        execute_at=None,
        now_iso=datetime.now(UTC).isoformat(),
        snooze_label="forever",
        new_token="tok-new",
        audit_detail="kept",
        actor="admin",
    )


def _clear_scheduled(other, media_id):
    from mediaman.scanner.repository import clear_pending_deletions

    clear_pending_deletions(other)


def _keep_show(other, media_id):
    from mediaman.web.repository.kept import set_protected_state

    action_id = other.execute(
        "SELECT id FROM scheduled_actions WHERE media_item_id=?", (media_id,)
    ).fetchone()["id"]
    now_iso = datetime.now(UTC).isoformat()
    with other:
        set_protected_state(
            other,
            to_update=[("protected_forever", None, now_iso, "forever", action_id)],
            to_insert=[],
        )


class TestChangeDuringPass:
    @pytest.mark.parametrize("change", [_keep_forever, _clear_scheduled, _keep_show])
    def test_keep_or_clear_made_mid_pass_wins(self, conn, db_path, tmp_path, monkeypatch, change):
        root = tmp_path / "movies"
        root.mkdir()
        first, second = root / "A.mkv", root / "B.mkv"
        first.write_bytes(b"x")
        second.write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="a", plex_rating_key="a", file_path=str(first), radarr_id=1)
        insert_media_item(conn, id="b", plex_rating_key="b", file_path=str(second), radarr_id=2)
        _track(conn, "a", first)
        _track(conn, "b", second)
        _due_row(conn, "a")
        _due_row(conn, "b")
        other = init_db(str(db_path))

        def unmonitor(radarr_id):
            if radarr_id == 1:
                change(other, "b")

        radarr = MagicMock()
        radarr.unmonitor_movie.side_effect = unmonitor

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert not first.exists()
        assert second.exists()
        assert result["deleted"] == 1
        assert "deleted" not in _audits(conn, "b")
        assert "scheduled_deletion" not in _actions(conn, "b")


class TestSharedFolder:
    """A season of a show without season folders points at the show folder."""

    def _show(self, tmp_path, monkeypatch):
        root = tmp_path / "tv"
        show = root / "Show"
        show.mkdir(parents=True)
        (show / "Show - s01e01.mkv").write_bytes(b"x")
        (show / "Show - s02e01.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        return root, show

    def _season(self, conn, media_id, path, number, size=10, files=None):
        """Insert a season stored at *path*, tracking *files* or its own episode there."""
        insert_media_item(
            conn,
            id=media_id,
            title="Show",
            plex_rating_key=media_id,
            media_type="season",
            season_number=number,
            sonarr_id=7,
            file_path=str(path),
            file_size_bytes=size,
        )
        _track(conn, media_id, *(files or [f"{path}/Show - s0{number}e01.mkv"]))

    @pytest.mark.parametrize(
        "other_action",
        ["protected_forever", "snoozed", None],
        ids=["kept", "snoozed", "unscheduled"],
    )
    def test_folder_holding_another_items_media_is_not_deleted(
        self, conn, tmp_path, monkeypatch, other_action
    ):
        _, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", show, 1)
        self._season(conn, "s2", show, 2)
        _due_row(conn, "s1")
        if other_action:
            insert_scheduled_action(
                conn,
                media_item_id="s2",
                action=other_action,
                token="tok-s2",
                execute_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
                token_used=other_action == "protected_forever",
            )
        sonarr = MagicMock()

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert (show / "Show - s02e01.mkv").exists()
        assert (show / "Show - s01e01.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='s1'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")
        assert _audits(conn, "s1") == ["delete_refused"]
        sonarr.unmonitor_season.assert_not_called()

    def test_folder_holding_media_in_a_subfolder_is_not_deleted(self, conn, tmp_path, monkeypatch):
        _, show = self._show(tmp_path, monkeypatch)
        extras = show / "Season 3"
        extras.mkdir()
        (extras / "Show - s03e01.mkv").write_bytes(b"x")
        self._season(conn, "s1", show, 1)
        self._season(conn, "s3", extras, 3)
        _due_row(conn, "s1")

        DeletionExecutor(conn=conn).execute()

        assert (extras / "Show - s03e01.mkv").exists()
        assert _audits(conn, "s1") == ["delete_refused"]

    def test_folder_sharing_only_a_name_prefix_does_not_block(self, conn, tmp_path, monkeypatch):
        root, show = self._show(tmp_path, monkeypatch)
        neighbour = root / "Show 2"
        neighbour.mkdir()
        (neighbour / "Show 2 - s01e01.mkv").write_bytes(b"x")
        self._season(conn, "s1", show, 1)
        self._season(conn, "n1", neighbour, 1)
        _due_row(conn, "s1")

        result = DeletionExecutor(conn=conn).execute()

        assert not (show / "Show - s01e01.mkv").exists()
        assert (show / "Show - s02e01.mkv").exists()
        assert (neighbour / "Show 2 - s01e01.mkv").exists()
        assert result["deleted"] == 1

    def test_seasons_due_together_delete_the_folder_once_and_all_close(
        self, conn, tmp_path, monkeypatch
    ):
        # The show is the root's only folder, so the root is empty once it goes:
        # the second season must still close as deleted, not wait as unmounted.
        _, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", show, 1, size=10)
        self._season(conn, "s2", show, 2, size=20)
        _due_row(conn, "s1")
        _due_row(conn, "s2")
        sonarr = MagicMock()

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 2}
        assert _actions(conn, "s1") == [] and _actions(conn, "s2") == []
        assert _audits(conn, "s1") == ["deleted"] and _audits(conn, "s2") == ["deleted"]
        assert sorted(c.args for c in sonarr.unmonitor_season.call_args_list) == [(7, 1), (7, 2)]

    @pytest.mark.parametrize("first", ["s2", "s1"], ids=["show-first", "season-folder-first"])
    def test_due_show_folder_and_due_season_folder_inside_it_both_close(
        self, conn, tmp_path, monkeypatch, first
    ):
        _, show = self._show(tmp_path, monkeypatch)
        season_one = show / "Season 1"
        season_one.mkdir()
        (season_one / "Show - s01e02.mkv").write_bytes(b"x")
        self._season(conn, "s1", season_one, 1, size=10, files=[season_one / "Show - s01e02.mkv"])
        self._season(
            conn,
            "s2",
            show,
            2,
            size=20,
            files=[show / "Show - s01e01.mkv", show / "Show - s02e01.mkv"],
        )
        for media_id in (first, "s1" if first == "s2" else "s2"):
            _due_row(conn, media_id)

        result = DeletionExecutor(conn=conn).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 3}
        assert _actions(conn, "s1") == [] and _actions(conn, "s2") == []
        assert _audits(conn, "s1") == ["deleted"] and _audits(conn, "s2") == ["deleted"]

    @pytest.mark.parametrize("first", ["s2", "s1"], ids=["show-first", "inner-first"])
    def test_due_inner_path_stored_with_a_double_slash_does_not_hold_the_folder_back(
        self, conn, tmp_path, monkeypatch, first
    ):
        root, show = self._show(tmp_path, monkeypatch)
        season_one = show / "Season 1"
        season_one.mkdir()
        (season_one / "Show - s01e02.mkv").write_bytes(b"x")
        # Raw, "//Show/..." sorts before "/Show", so the inner path used to go first.
        self._season(
            conn,
            "s1",
            f"{root}//Show/Season 1",
            1,
            size=10,
            files=[season_one / "Show - s01e02.mkv"],
        )
        self._season(
            conn,
            "s2",
            show,
            2,
            size=20,
            files=[show / "Show - s01e01.mkv", show / "Show - s02e01.mkv"],
        )
        for media_id in (first, "s1" if first == "s2" else "s2"):
            _due_row(conn, media_id)

        result = DeletionExecutor(conn=conn).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 3}
        assert _actions(conn, "s1") == [] and _actions(conn, "s2") == []
        assert _audits(conn, "s1") == ["deleted"] and _audits(conn, "s2") == ["deleted"]

    def test_keep_on_a_co_located_due_item_during_removal_is_refused(
        self, conn, db_path, tmp_path, monkeypatch
    ):
        from mediaman.scanner import deletions
        from mediaman.services.scheduled_actions import apply_keep_forever

        _, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", show, 1)
        self._season(conn, "s2", show, 2)
        _due_row(conn, "s1")
        _due_row(conn, "s2")
        other = init_db(str(db_path))
        s2 = other.execute(
            "SELECT id, execute_at FROM scheduled_actions WHERE media_item_id='s2'"
        ).fetchone()
        kept = []
        real_delete = deletions.delete_tracked_files

        def keep_then_delete(tracked, **kwargs):
            # Timed at s2's own deadline so only the pass's claim can refuse it.
            keep_time = datetime.fromisoformat(s2["execute_at"])
            with other:
                kept.append(apply_keep_forever(other, s2["id"], keep_time))
            return real_delete(tracked, **kwargs)

        monkeypatch.setattr(deletions, "delete_tracked_files", keep_then_delete)

        result = DeletionExecutor(conn=conn).execute()

        assert kept == [0]
        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 2}
        assert _actions(conn, "s2") == []
        assert _audits(conn, "s2") == ["deleted"]

    def test_failed_removal_returns_co_located_rows_to_pending(self, conn, tmp_path, monkeypatch):
        from mediaman.scanner import deletions
        from mediaman.services.infra import DeletionRefused

        _, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", show, 1)
        self._season(conn, "s2", show, 2)
        _due_row(conn, "s1")
        _due_row(conn, "s2")

        def refuse(tracked, **kwargs):
            raise DeletionRefused("refused")

        monkeypatch.setattr(deletions, "delete_tracked_files", refuse)

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 0
        statuses = conn.execute("SELECT DISTINCT delete_status FROM scheduled_actions").fetchall()
        assert [r[0] for r in statuses] == ["pending"]


class TestMissingTarget:
    @pytest.mark.parametrize("root_state", ["missing", "empty"])
    def test_unavailable_mount_leaves_the_row_pending(
        self, conn, tmp_path, monkeypatch, root_state
    ):
        root = tmp_path / "movies"
        if root_state == "empty":
            root.mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"), radarr_id=5
        )
        _track(conn, "m", root / "M" / "M.mkv")
        _due_row(conn, "m")
        radarr = MagicMock()

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")
        assert "deleted" not in _audits(conn, "m")
        radarr.unmonitor_movie.assert_not_called()

    @pytest.mark.parametrize("root_state", ["missing", "empty"])
    def test_mount_dropping_after_the_check_leaves_the_row_pending(
        self, conn, tmp_path, monkeypatch, root_state
    ):
        import shutil

        from mediaman.scanner import deletions

        root = tmp_path / "movies"
        (root / "M").mkdir(parents=True)
        (root / "M" / "M.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"), radarr_id=5
        )
        _track(conn, "m", root / "M" / "M.mkv")
        _due_row(conn, "m")
        real_claim = deletions.claim_due_deletions_under

        def claim_then_unmount(*args):
            claimed = real_claim(*args)
            shutil.rmtree(root)
            if root_state == "empty":
                root.mkdir()
            return claimed

        monkeypatch.setattr(deletions, "claim_due_deletions_under", claim_then_unmount)
        radarr = MagicMock()

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")
        assert "deleted" not in _audits(conn, "m")
        radarr.unmonitor_movie.assert_not_called()

    def test_dropped_mount_below_the_root_leaves_the_row_pending(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "media"
        (root / "tv" / "local" / "Other").mkdir(parents=True)
        (root / "tv" / "local" / "Other" / "Other.mkv").write_bytes(b"x")
        (root / "tv" / "nas").mkdir()
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn, id="m", plex_rating_key="m", file_path=str(root / "tv" / "nas" / "Show")
        )
        _track(conn, "m", root / "tv" / "nas" / "Show" / "Show - s01e01.mkv")
        _due_row(conn, "m")

        result = DeletionExecutor(conn=conn).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")
        assert "deleted" not in _audits(conn, "m")

    def test_file_gone_while_mount_is_up_is_not_booked_deleted(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "movies"
        (root / "Other").mkdir(parents=True)
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"))
        _track(conn, "m", root / "M" / "M.mkv")
        _due_row(conn, "m")

        result = DeletionExecutor(conn=conn).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")
        assert _audits(conn, "m") == ["delete_incomplete"]
        assert (root / "Other" / "Other.mkv").exists()


class TestRootUnavailableAudit:
    def _audit_rows(self, conn, media_id):
        return conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id=? AND action='delete_root_unavailable'",
            (media_id,),
        ).fetchall()

    @pytest.mark.parametrize("layout", ["empty_movie_folder", "missing_root"])
    def test_a_row_held_back_on_every_pass_is_audited_once_naming_the_folder(
        self, conn, tmp_path, monkeypatch, layout
    ):
        root = tmp_path / "movies"
        folder = root / "Dune (2021)"
        if layout == "empty_movie_folder":
            folder.mkdir(parents=True)
            empty = folder
        else:
            empty = root
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(folder / "Dune.mkv"))
        _track(conn, "m", folder / "Dune.mkv")
        _due_row(conn, "m")

        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        audits = self._audit_rows(conn, "m")
        assert len(audits) == 1
        assert str(empty) in audits[0]["detail"]
        assert "deleted" not in _audits(conn, "m")
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")

    def test_a_gone_file_outside_the_delete_roots_is_refused_not_blamed_on_the_mount(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        (root / "Other").mkdir(parents=True)
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn, id="m", plex_rating_key="m", file_path=str(tmp_path / "old" / "M" / "M.mkv")
        )
        _track(conn, "m", tmp_path / "old" / "M" / "M.mkv")
        _due_row(conn, "m")

        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        assert _audits(conn, "m") == ["delete_refused"]
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert (row["action"], row["delete_status"]) == ("scheduled_deletion", "pending")

    def test_recovery_holding_a_row_back_audits_it_once_naming_the_folder(
        self, conn, tmp_path, monkeypatch
    ):
        folder = tmp_path / "movies" / "Dune (2021)"
        folder.mkdir(parents=True)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(folder.parent))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(folder / "Dune.mkv"))
        _track(conn, "m", folder / "Dune.mkv")
        insert_scheduled_action(
            conn,
            media_item_id="m",
            action="scheduled_deletion",
            token="tok-m",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)
        audits = self._audit_rows(conn, "m")
        DeletionExecutor(conn=conn).execute()

        assert len(audits) == 1
        assert str(folder) in audits[0]["detail"]
        assert len(self._audit_rows(conn, "m")) == 1
        assert "deleted" not in _audits(conn, "m")
        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions WHERE media_item_id='m'"
        ).fetchone()
        assert row["delete_status"] == "pending"


class TestSharedFolderPathSpelling:
    """Two spellings of one folder are the same folder."""

    def _show(self, tmp_path, monkeypatch):
        root = tmp_path / "tv"
        show = root / "Show"
        show.mkdir(parents=True)
        (show / "Show - s01e01.mkv").write_bytes(b"x")
        (show / "Show - s02e01.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        return root, show

    def _season(self, conn, media_id, path, number):
        insert_media_item(
            conn,
            id=media_id,
            title="Show",
            plex_rating_key=media_id,
            media_type="season",
            season_number=number,
            file_path=path,
            file_size_bytes=10,
        )
        _track(conn, media_id, os.path.normpath(f"{path}/Show - s0{number}e01.mkv"))

    @pytest.mark.parametrize("doubled", ["sibling", "target"])
    def test_kept_sibling_spelled_differently_blocks_the_removal(
        self, conn, tmp_path, monkeypatch, doubled
    ):
        root, show = self._show(tmp_path, monkeypatch)
        plain, double = str(show), f"{root}//Show"
        self._season(conn, "s1", double if doubled == "target" else plain, 1)
        self._season(conn, "s2", double if doubled == "sibling" else plain, 2)
        _due_row(conn, "s1")
        insert_scheduled_action(
            conn,
            media_item_id="s2",
            action="protected_forever",
            token="tok-s2",
            token_used=True,
        )

        result = DeletionExecutor(conn=conn).execute()

        assert (show / "Show - s02e01.mkv").exists()
        assert result["deleted"] == 0

    @pytest.mark.parametrize("spelling", ["{root}/./Show", "{root}/x/../Show"])
    def test_kept_sibling_spelled_with_dot_segments_blocks_the_removal(
        self, conn, tmp_path, monkeypatch, spelling
    ):
        root, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", str(show), 1)
        self._season(conn, "s2", spelling.format(root=root), 2)
        _due_row(conn, "s1")

        result = DeletionExecutor(conn=conn).execute()

        assert (show / "Show - s02e01.mkv").exists()
        assert result["deleted"] == 0

    def test_kept_media_reached_through_a_symlink_and_dot_dots_blocks_the_removal(
        self, conn, tmp_path, monkeypatch
    ):
        # Normalised, the stored path reads <root>/ep, outside Show; on disk the
        # link makes it Show/ep, so only its stored spelling shows it is inside.
        _, show = self._show(tmp_path, monkeypatch)
        (show / "a" / "b").mkdir(parents=True)
        (show / "link").symlink_to(show / "a" / "b")
        (show / "ep").write_bytes(b"x")
        self._season(conn, "s1", str(show), 1)
        self._season(conn, "s2", f"{show}/link/../../ep", 2)
        _due_row(conn, "s1")

        result = DeletionExecutor(conn=conn).execute()

        assert (show / "ep").exists()
        assert result["deleted"] == 0

    def test_due_sibling_spelled_differently_closes_with_the_folder(
        self, conn, tmp_path, monkeypatch
    ):
        root, show = self._show(tmp_path, monkeypatch)
        self._season(conn, "s1", str(show), 1)
        self._season(conn, "s2", f"{root}//Show", 2)
        _due_row(conn, "s1")
        _due_row(conn, "s2")

        result = DeletionExecutor(conn=conn).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 2}
        assert _actions(conn, "s2") == []


class TestDiskChecksOutsideWriteLock:
    """A hung media mount must not hold the database write lock."""

    def _record_transaction_state(self, monkeypatch, conn, seen):
        import os

        from mediaman.scanner import deletions

        real_lexists = os.path.lexists
        real_unavailable_folder = deletions.unavailable_folder

        def lexists(path):
            seen.append(conn.in_transaction)
            return real_lexists(path)

        def unavailable_folder(path, roots):
            seen.append(conn.in_transaction)
            return real_unavailable_folder(path, roots)

        monkeypatch.setattr(os.path, "lexists", lexists)
        monkeypatch.setattr(deletions, "unavailable_folder", unavailable_folder)

    def test_pass_checks_a_missing_target_with_no_transaction_open(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        (root / "Other").mkdir(parents=True)
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"))
        _track(conn, "m", root / "M" / "M.mkv")
        _due_row(conn, "m")
        seen = []
        self._record_transaction_state(monkeypatch, conn, seen)

        DeletionExecutor(conn=conn).execute()

        assert seen and not any(seen)

    def test_recovery_checks_each_stuck_row_with_no_transaction_open(
        self, conn, tmp_path, monkeypatch
    ):
        for media_id in ("a", "b"):
            target = tmp_path / f"{media_id}.mkv"
            target.write_bytes(b"x")
            insert_media_item(conn, id=media_id, plex_rating_key=media_id, file_path=str(target))
            _track(conn, media_id, target)
            insert_scheduled_action(
                conn,
                media_item_id=media_id,
                action="scheduled_deletion",
                token=f"tok-{media_id}",
                execute_at="2020-01-01",
                token_used=False,
                delete_status="deleting",
            )
        conn.commit()
        seen = []
        self._record_transaction_state(monkeypatch, conn, seen)

        recover_stuck_deletions(conn)

        assert len(seen) == 2 and not any(seen)


class TestRefusalAudit:
    def _flat_show(self, conn, tmp_path, monkeypatch, *, kept):
        root = tmp_path / "tv"
        show = root / "Show"
        show.mkdir(parents=True)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        for number in (1, *kept):
            (show / f"Show - s0{number}e01.mkv").write_bytes(b"x")
            insert_media_item(
                conn,
                id=f"s{number}",
                title="Show",
                plex_rating_key=f"s{number}",
                media_type="season",
                season_number=number,
                file_path=str(show),
            )
            _track(conn, f"s{number}", show / f"Show - s0{number}e01.mkv")
        _due_row(conn, "s1")
        return show

    def test_a_row_refused_on_every_pass_is_audited_once(self, conn, tmp_path, monkeypatch):
        self._flat_show(conn, tmp_path, monkeypatch, kept=[2])

        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        assert _audits(conn, "s1") == ["delete_refused"]

    def test_a_refusal_for_a_new_reason_is_audited_again(self, conn, tmp_path, monkeypatch):
        show = self._flat_show(conn, tmp_path, monkeypatch, kept=[2])
        DeletionExecutor(conn=conn).execute()
        (show / "Show - s03e01.mkv").write_bytes(b"x")
        insert_media_item(
            conn,
            id="s3",
            title="Show",
            plex_rating_key="s3",
            media_type="season",
            season_number=3,
            file_path=str(show),
        )

        DeletionExecutor(conn=conn).execute()

        assert _audits(conn, "s1") == ["delete_refused", "delete_refused"]


class TestSafetyRefusalAudit:
    def test_a_refused_removal_is_audited_once_with_its_reason(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "library"
        root.mkdir()
        (root / "movie.mkv").write_text("keep")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        _insert_media(conn, file_path=str(root))
        _insert_pending_deletion(conn)

        DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        rows = conn.execute(
            "SELECT action, detail FROM audit_log WHERE media_item_id='m1'"
        ).fetchall()
        assert [r["action"] for r in rows] == ["delete_refused"]
        assert "strict descendant" in rows[0]["detail"]
        assert (root / "movie.mkv").exists()


class TestKeepAndClearAroundARemoval:
    """A keep or clear pressed while the files are being removed must win if the removal fails."""

    @pytest.fixture
    def movie(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "movies"
        folder = root / "Movie"
        folder.mkdir(parents=True)
        (folder / "Movie.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m1", plex_rating_key="m1", file_path=str(folder))
        _track(conn, "m1", folder / "Movie.mkv")
        _due_row(conn, "m1")
        return folder

    def _row(self, conn):
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchone()
        return None if row is None else (row["action"], row["delete_status"])

    def _failing_pass(self, conn, monkeypatch, error, during=None):
        calls = []

        def fail(tracked, **kwargs):
            calls.append(tracked)
            if during is not None:
                during()
            raise error

        with monkeypatch.context() as patch:
            patch.setattr("mediaman.scanner.deletions.delete_tracked_files", fail)
            DeletionExecutor(conn=conn).execute()
        return calls

    @pytest.mark.parametrize("keep", [_keep_forever, _keep_show], ids=["library", "show"])
    def test_keep_during_a_removal_that_fails_saves_the_media(
        self, conn, db_path, monkeypatch, movie, keep
    ):
        other = init_db(str(db_path))

        self._failing_pass(
            conn, monkeypatch, PermissionError("read-only"), during=lambda: keep(other, "m1")
        )
        DeletionExecutor(conn=conn).execute()

        assert movie.exists()
        assert self._row(conn) == ("protected_forever", "pending")
        assert "deleted" not in _audits(conn, "m1")

    @pytest.mark.parametrize("keep", [_keep_forever, _keep_show], ids=["library", "show"])
    def test_keep_during_a_removal_that_succeeds_is_closed_with_it(
        self, conn, db_path, monkeypatch, movie, keep
    ):
        from mediaman.scanner import deletions

        other = init_db(str(db_path))
        real = deletions.delete_tracked_files
        seen = {}

        def remove(tracked, **kwargs):
            keep(other, "m1")
            seen["row"] = self._row(other)
            return real(tracked, **kwargs)

        monkeypatch.setattr(deletions, "delete_tracked_files", remove)

        result = DeletionExecutor(conn=conn).execute()

        assert seen == {"row": ("protected_forever", "deleting")}
        assert not movie.exists()
        assert result["deleted"] == 1
        assert self._row(conn) is None
        assert "deleted" in _audits(conn, "m1")

    @pytest.mark.parametrize(
        "error", [PermissionError("read-only"), OSError("i/o"), RuntimeError("bug")]
    )
    def test_a_failed_removal_goes_back_to_pending_and_the_next_pass_retries(
        self, conn, monkeypatch, movie, error
    ):
        calls = self._failing_pass(conn, monkeypatch, error)

        assert len(calls) == 1
        assert self._row(conn) == ("scheduled_deletion", "pending")
        assert _audits(conn, "m1") == ["delete_failed_permanent"]
        assert movie.exists()

        DeletionExecutor(conn=conn).execute()

        assert not movie.exists()
        assert self._row(conn) is None

    def test_keep_after_a_failed_removal_saves_the_media(self, conn, monkeypatch, movie):
        self._failing_pass(conn, monkeypatch, PermissionError("read-only"))

        _keep_forever(conn, "m1")
        DeletionExecutor(conn=conn).execute()

        assert movie.exists()
        assert self._row(conn) == ("protected_forever", "pending")
        assert "deleted" not in _audits(conn, "m1")

    def test_clear_during_a_removal_that_fails_cancels_it_and_counts_it_apart(
        self, conn, db_path, monkeypatch, movie
    ):
        from mediaman.scanner.repository import clear_pending_deletions

        insert_media_item(conn, id="later", plex_rating_key="later", file_path=f"{movie}2")
        _insert_pending_deletion(
            conn, media_id="later", execute_at=(datetime.now(UTC) + timedelta(days=3)).isoformat()
        )
        other = init_db(str(db_path))
        seen = {}

        def clear():
            seen["result"] = clear_pending_deletions(other)

        self._failing_pass(conn, monkeypatch, PermissionError("read-only"), during=clear)
        DeletionExecutor(conn=conn).execute()

        assert movie.exists()
        assert self._row(conn) is None
        assert "deleted" not in _audits(conn, "m1")
        assert _actions(conn, "later") == []
        assert seen["result"] == (1, 1)

    def test_clear_after_a_failed_removal_cancels_the_retry(self, conn, monkeypatch, movie):
        from mediaman.scanner.repository import clear_pending_deletions

        self._failing_pass(conn, monkeypatch, PermissionError("read-only"))

        assert clear_pending_deletions(conn) == (1, 0)
        DeletionExecutor(conn=conn).execute()

        assert movie.exists()
        assert self._row(conn) is None
        assert "deleted" not in _audits(conn, "m1")

    def test_recovery_of_a_kept_row_whose_files_are_gone_audits_it_and_keeps_the_row(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        root.mkdir()
        (root / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn,
            id="m1",
            plex_rating_key="m1",
            file_path=str(root / "Gone.mkv"),
            file_size_bytes=500,
        )
        _track(conn, "m1", root / "Gone.mkv")
        insert_scheduled_action(
            conn,
            media_item_id="m1",
            action="protected_forever",
            token="tok-m1",
            execute_at=None,
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        assert self._row(conn) == ("protected_forever", "pending")
        audits = conn.execute(
            "SELECT action, space_reclaimed_bytes FROM audit_log WHERE media_item_id='m1'"
        ).fetchall()
        assert [tuple(a) for a in audits] == [("media_missing", None)]


class TestKeepBetweenClaimAndRemoval:
    """A keep landing while the pass checks the disk after its claim must stop the removal."""

    def _change_once_claimed(self, monkeypatch, other, path, media_id, change):
        """Run *change* the first time the pass checks tracked file *path* while *media_id* is claimed."""
        import os

        real = os.path.lexists
        done = []

        def lexists(candidate):
            claimed = other.execute(
                "SELECT 1 FROM scheduled_actions WHERE media_item_id=? AND delete_status='deleting'",
                (media_id,),
            ).fetchone()
            if not done and claimed and os.fspath(candidate) == str(path):
                done.append(media_id)
                change(other, media_id)
            return real(candidate)

        monkeypatch.setattr(os.path, "lexists", lexists)
        return done

    def _row(self, conn, media_id):
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id=?",
            (media_id,),
        ).fetchone()
        return None if row is None else (row["action"], row["delete_status"])

    def _movie(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "movies"
        folder = root / "Movie"
        folder.mkdir(parents=True)
        (folder / "Movie.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m1", plex_rating_key="m1", file_path=str(folder), radarr_id=1)
        _track(conn, "m1", folder / "Movie.mkv")
        _due_row(conn, "m1")
        return folder

    @pytest.mark.parametrize("keep", [_keep_forever, _keep_show], ids=["library", "show"])
    def test_keep_after_the_claim_leaves_the_media_and_keeps_the_row(
        self, conn, db_path, tmp_path, monkeypatch, keep
    ):
        folder = self._movie(conn, tmp_path, monkeypatch)
        other = init_db(str(db_path))
        done = self._change_once_claimed(monkeypatch, other, folder / "Movie.mkv", "m1", keep)
        radarr = MagicMock()

        result = DeletionExecutor(conn=conn, radarr_client=radarr).execute()

        assert done == ["m1"]
        assert (folder / "Movie.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert self._row(conn, "m1") == ("protected_forever", "pending")
        assert "deleted" not in _audits(conn, "m1")
        radarr.unmonitor_movie.assert_not_called()

    def test_clear_after_the_claim_leaves_the_media(self, conn, db_path, tmp_path, monkeypatch):
        folder = self._movie(conn, tmp_path, monkeypatch)
        other = init_db(str(db_path))
        done = self._change_once_claimed(
            monkeypatch, other, folder / "Movie.mkv", "m1", _clear_scheduled
        )

        result = DeletionExecutor(conn=conn).execute()

        assert done == ["m1"]
        assert (folder / "Movie.mkv").exists()
        assert result["deleted"] == 0
        assert self._row(conn, "m1") is None
        assert "deleted" not in _audits(conn, "m1")

    def test_keep_on_a_co_located_item_after_the_claim_leaves_the_shared_folder(
        self, conn, db_path, tmp_path, monkeypatch
    ):
        root = tmp_path / "tv"
        show = root / "Show"
        show.mkdir(parents=True)
        (show / "Show - s01e01.mkv").write_bytes(b"x")
        (show / "Show - s02e01.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        for media_id, number in (("s1", 1), ("s2", 2)):
            insert_media_item(
                conn,
                id=media_id,
                title="Show",
                plex_rating_key=media_id,
                media_type="season",
                season_number=number,
                sonarr_id=7,
                file_path=str(show),
                file_size_bytes=10,
            )
            _track(conn, media_id, show / f"Show - s0{number}e01.mkv")
            _due_row(conn, media_id)
        other = init_db(str(db_path))
        done = self._change_once_claimed(
            monkeypatch, other, show / "Show - s01e01.mkv", "s2", _keep_forever
        )
        sonarr = MagicMock()

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert done == ["s2"]
        assert (show / "Show - s01e01.mkv").exists()
        assert (show / "Show - s02e01.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert self._row(conn, "s1") == ("scheduled_deletion", "pending")
        assert self._row(conn, "s2") == ("protected_forever", "pending")
        assert "deleted" not in _audits(conn, "s1") + _audits(conn, "s2")
        sonarr.unmonitor_season.assert_not_called()

        DeletionExecutor(conn=conn).execute()

        assert (show / "Show - s02e01.mkv").exists()
        assert self._row(conn, "s1") == ("scheduled_deletion", "pending")

    def _season(self, conn, media_id, number, folder, show_key):
        (folder / f"Show - s0{number}e01.mkv").write_bytes(b"x")
        insert_media_item(
            conn,
            id=media_id,
            title="Show",
            plex_rating_key=media_id,
            media_type="season",
            season_number=number,
            sonarr_id=7,
            show_rating_key=show_key,
            file_path=str(folder),
        )
        _track(conn, media_id, folder / f"Show - s0{number}e01.mkv")
        _due_row(conn, media_id)

    @staticmethod
    def _keep_whole_show(other, media_id):
        insert_kept_show(other, show_rating_key="show1", action="protected_forever")
        other.commit()

    def test_show_keep_after_the_claim_leaves_the_media_and_the_next_pass_cancels_it(
        self, conn, db_path, tmp_path, monkeypatch
    ):
        root = tmp_path / "tv"
        folder = root / "Show" / "Season 2"
        folder.mkdir(parents=True)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        self._season(conn, "s2", 2, folder, "show1")
        other = init_db(str(db_path))
        done = self._change_once_claimed(
            monkeypatch, other, folder / "Show - s02e01.mkv", "s2", self._keep_whole_show
        )
        sonarr = MagicMock()

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert done == ["s2"]
        assert (folder / "Show - s02e01.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert self._row(conn, "s2") == ("scheduled_deletion", "pending")
        assert "deleted" not in _audits(conn, "s2")
        sonarr.unmonitor_season.assert_not_called()

        DeletionExecutor(conn=conn).execute()

        assert (folder / "Show - s02e01.mkv").exists()
        assert self._row(conn, "s2") is None
        assert _audits(conn, "s2") == ["delete_refused"]

    def test_show_keep_on_a_co_located_item_after_the_claim_leaves_the_shared_folder(
        self, conn, db_path, tmp_path, monkeypatch
    ):
        root = tmp_path / "tv"
        show = root / "Show"
        show.mkdir(parents=True)
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        self._season(conn, "s1", 1, show, "other")
        self._season(conn, "s2", 2, show, "show1")
        other = init_db(str(db_path))
        done = self._change_once_claimed(
            monkeypatch, other, show / "Show - s01e01.mkv", "s2", self._keep_whole_show
        )

        result = DeletionExecutor(conn=conn).execute()

        assert done == ["s2"]
        assert (show / "Show - s01e01.mkv").exists()
        assert (show / "Show - s02e01.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert self._row(conn, "s1") == ("scheduled_deletion", "pending")
        # Put back to pending, s2's own turn later in the same pass cancels it.
        assert self._row(conn, "s2") is None
        assert _audits(conn, "s2") == ["delete_refused"]
        assert "deleted" not in _audits(conn, "s1")

        DeletionExecutor(conn=conn).execute()

        assert (show / "Show - s02e01.mkv").exists()
        assert self._row(conn, "s1") == ("scheduled_deletion", "pending")


class TestKeptItemIsNotDeleted:
    """The pass keeps what a scan keeps, even when a due deletion row is left beside the keep."""

    def _season(self, conn, tmp_path, monkeypatch, media_id="s1", number=1, folder=None):
        root = tmp_path / "tv"
        folder = folder or root / "Show" / f"Season {number}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Show - s0{number}e01.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(
            conn,
            id=media_id,
            title="Show",
            plex_rating_key=media_id,
            media_type="season",
            season_number=number,
            show_rating_key="show1",
            file_path=str(folder),
        )
        _track(conn, media_id, folder / f"Show - s0{number}e01.mkv")
        _due_row(conn, media_id)
        return folder

    def _keep_show(self, conn, *, execute_at):
        insert_kept_show(
            conn,
            show_rating_key="show1",
            action="snoozed" if execute_at else "protected_forever",
            execute_at=execute_at,
        )
        conn.commit()

    @pytest.mark.parametrize("keep", ["forever", "snoozed", "show"])
    def test_a_live_keep_beside_a_due_deletion_cancels_it_and_leaves_the_files(
        self, conn, tmp_path, monkeypatch, keep
    ):
        folder = self._season(conn, tmp_path, monkeypatch)
        later = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        if keep == "show":
            self._keep_show(conn, execute_at=None)
        else:
            insert_scheduled_action(
                conn,
                media_item_id="s1",
                action="protected_forever" if keep == "forever" else "snoozed",
                token="tok-keep",
                execute_at=None if keep == "forever" else later,
            )
        sonarr = MagicMock()

        result = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert folder.exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert "scheduled_deletion" not in _actions(conn, "s1")
        assert _audits(conn, "s1") == ["delete_refused"]
        sonarr.unmonitor_season.assert_not_called()

    @pytest.mark.parametrize("keep", ["show", "snoozed"])
    def test_a_lapsed_keep_does_not_block_a_due_deletion(self, conn, tmp_path, monkeypatch, keep):
        folder = self._season(conn, tmp_path, monkeypatch)
        earlier = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        if keep == "show":
            self._keep_show(conn, execute_at=earlier)
        else:
            insert_scheduled_action(
                conn, media_item_id="s1", action="snoozed", token="tok-keep", execute_at=earlier
            )

        result = DeletionExecutor(conn=conn).execute()

        assert not folder.exists()
        assert result["deleted"] == 1
        assert _audits(conn, "s1") == ["deleted"]

    def test_a_kept_item_sharing_the_folder_of_a_due_one_keeps_the_folder(
        self, conn, tmp_path, monkeypatch
    ):
        show = tmp_path / "tv" / "Show"
        self._season(conn, tmp_path, monkeypatch, "s1", 1, folder=show)
        self._season(conn, tmp_path, monkeypatch, "s2", 2, folder=show)
        conn.execute("UPDATE media_items SET show_rating_key = 'other' WHERE id = 's1'")
        self._keep_show(conn, execute_at=None)

        result = DeletionExecutor(conn=conn).execute()

        assert (show / "Show - s01e01.mkv").exists()
        assert (show / "Show - s02e01.mkv").exists()
        assert result["deleted"] == 0
        assert "scheduled_deletion" not in _actions(conn, "s2")


class TestTrackedFiles:
    """Only the files a scan recorded for the item go, with their sidecars."""

    def _root(self, tmp_path, monkeypatch, name="tv"):
        root = tmp_path / name
        root.mkdir()
        (root / "Other").mkdir()
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        return root

    def _season(self, conn, folder, media_id="s1", number=1):
        insert_media_item(
            conn,
            id=media_id,
            title="Show",
            plex_rating_key=media_id,
            media_type="season",
            season_number=number,
            file_path=str(folder),
        )
        _due_row(conn, media_id)

    def _status(self, conn, media_id):
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id=?",
            (media_id,),
        ).fetchone()
        return None if row is None else (row["action"], row["delete_status"])

    def _deleted_media(self, conn):
        return conn.execute("SELECT * FROM deleted_media").fetchall()

    def test_a_season_keeps_its_untracked_episode_and_its_folder(self, conn, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        folder = root / "Show" / "Season 1"
        folder.mkdir(parents=True)
        (folder / "Show - s01e01.mkv").write_bytes(b"e" * 100)
        (folder / "Show - s01e01.en.srt").write_bytes(b"s" * 5)
        (folder / "Show - s01e02.mkv").write_bytes(b"u")
        (folder / "season01-poster.jpg").write_bytes(b"p")
        self._season(conn, folder)
        _track(conn, "s1", folder / "Show - s01e01.mkv")

        result = DeletionExecutor(conn=conn).execute()

        assert sorted(p.name for p in folder.iterdir()) == [
            "Show - s01e02.mkv",
            "season01-poster.jpg",
        ]
        assert result == {"deleted": 1, "reclaimed_bytes": 105}
        audit = conn.execute(
            "SELECT action, space_reclaimed_bytes FROM audit_log WHERE media_item_id='s1'"
        ).fetchone()
        assert tuple(audit) == ("deleted", 105)

    def test_sidecars_go_and_the_folder_left_with_only_artwork_goes(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch, "movies")
        folder = root / "Movie (2020)"
        folder.mkdir()
        for name, size in (
            ("Movie.mkv", 50),
            ("Movie.en.forced.srt", 3),
            ("Movie.nfo", 2),
            ("poster.jpg", 7),
        ):
            (folder / name).write_bytes(b"x" * size)
        _insert_media(conn, file_path=str(folder / "Movie.mkv"))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn).execute()

        assert not folder.exists()
        assert (root / "Other" / "Other.mkv").exists()
        assert result == {"deleted": 1, "reclaimed_bytes": 55}

    def test_a_file_another_item_tracks_is_not_taken_as_a_sidecar(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch, "movies")
        (root / "Movie.mkv").write_bytes(b"x")
        (root / "Movie.en.srt").write_bytes(b"x")
        _insert_media(conn, file_path=str(root / "Movie.mkv"))
        _insert_pending_deletion(conn)
        _insert_media(conn, media_id="other", file_path=str(root / "Movie.en.srt"))

        DeletionExecutor(conn=conn).execute()

        assert not (root / "Movie.mkv").exists()
        assert (root / "Movie.en.srt").exists()

    def test_a_tracked_file_another_item_also_tracks_stays_and_the_item_waits(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch, "movies")
        (root / "Movie").mkdir()
        (root / "Movie" / "Movie.mkv").write_bytes(b"x")
        _insert_media(conn, file_path=str(root / "Movie" / "Movie.mkv"))
        _insert_pending_deletion(conn)
        _insert_media(conn, media_id="other", file_path=str(root / "Other"))
        _track(conn, "other", root / "Movie" / "Movie.mkv")

        result = DeletionExecutor(conn=conn).execute()

        assert (root / "Movie" / "Movie.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert self._status(conn, "m1") == ("scheduled_deletion", "pending")
        assert _audits(conn, "m1") == ["delete_incomplete"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
    def test_a_file_that_cannot_be_removed_leaves_the_item_pending_with_the_bytes_freed(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch, "movies")
        free, locked = root / "Free", root / "Locked"
        free.mkdir()
        locked.mkdir()
        (free / "Movie - cd1.mkv").write_bytes(b"x" * 4)
        (locked / "Movie - cd2.mkv").write_bytes(b"x")
        _insert_media(conn, file_path=str(free / "Movie - cd1.mkv"))
        _track(conn, "m1", free / "Movie - cd1.mkv", locked / "Movie - cd2.mkv")
        _insert_pending_deletion(conn)
        locked.chmod(0o555)
        try:
            result = DeletionExecutor(conn=conn).execute()
        finally:
            locked.chmod(0o755)

        assert (locked / "Movie - cd2.mkv").exists()
        assert result == {"deleted": 0, "reclaimed_bytes": 4}
        assert self._status(conn, "m1") == ("scheduled_deletion", "pending")
        detail = conn.execute(
            "SELECT detail FROM audit_log WHERE media_item_id='m1' AND action='delete_incomplete'"
        ).fetchone()["detail"]
        assert "could not be removed" in detail

    def test_a_renamed_folder_stays_pending_with_one_audit_row(self, conn, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        old = root / "Show" / "Season 1"
        old.mkdir(parents=True)
        episodes = [old / "Show - s01e01.mkv", old / "Show - s01e02.mkv"]
        for episode in episodes:
            episode.write_bytes(b"x")
        self._season(conn, old)
        _track(conn, "s1", *episodes)
        renamed = old.rename(root / "Show" / "Season 01")
        sonarr = MagicMock()

        first = DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()
        DeletionExecutor(conn=conn, sonarr_client=sonarr).execute()

        assert first == {"deleted": 0, "reclaimed_bytes": 0}
        assert sorted(p.name for p in renamed.iterdir()) == [e.name for e in episodes]
        assert self._status(conn, "s1") == ("scheduled_deletion", "pending")
        audits = conn.execute(
            "SELECT action, detail FROM audit_log WHERE media_item_id='s1'"
        ).fetchall()
        assert [a["action"] for a in audits] == ["delete_incomplete"]
        assert "2 files" in audits[0]["detail"]
        assert self._deleted_media(conn) == []
        sonarr.unmonitor_season.assert_not_called()

    def test_a_partly_moved_item_counts_the_bytes_removed_and_stays_pending(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch, "movies")
        folder = root / "Movie"
        folder.mkdir()
        (folder / "Movie - cd1.mkv").write_bytes(b"x" * 10)
        _insert_media(conn, file_path=str(folder / "Movie - cd1.mkv"))
        _track(conn, "m1", folder / "Movie - cd1.mkv", folder / "Movie - cd2.mkv")
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 10}
        assert not (folder / "Movie - cd1.mkv").exists()
        assert self._status(conn, "m1") == ("scheduled_deletion", "pending")
        assert _audits(conn, "m1") == ["delete_incomplete"]

    def test_an_item_whose_files_are_not_recorded_waits_for_a_scan(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch)
        folder = root / "Show" / "Season 1"
        folder.mkdir(parents=True)
        (folder / "Show - s01e01.mkv").write_bytes(b"x")
        self._season(conn, folder)

        first = DeletionExecutor(conn=conn).execute()
        DeletionExecutor(conn=conn).execute()

        assert first == {"deleted": 0, "reclaimed_bytes": 0}
        assert (folder / "Show - s01e01.mkv").exists()
        assert self._status(conn, "s1") == ("scheduled_deletion", "pending")
        assert _audits(conn, "s1") == ["delete_awaiting_scan"]

    def test_a_co_located_item_with_unrecorded_files_is_not_closed_with_the_folder(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch)
        show = root / "Show"
        show.mkdir()
        (show / "Show - s01e01.mkv").write_bytes(b"x")
        (show / "Show - s02e01.mkv").write_bytes(b"x")
        self._season(conn, show, "s1", 1)
        self._season(conn, show, "s2", 2)
        _track(conn, "s1", show / "Show - s01e01.mkv")

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 1
        assert (show / "Show - s02e01.mkv").exists()
        assert self._status(conn, "s2") == ("scheduled_deletion", "pending")
        assert _audits(conn, "s2") == ["delete_awaiting_scan"]

    def test_deleted_media_is_written_with_the_deleted_audit_row(self, conn, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        folder = root / "Show" / "Season 2"
        folder.mkdir(parents=True)
        (folder / "Show - s02e01.mkv").write_bytes(b"x")
        self._season(conn, folder, "s2", 2)
        _track(conn, "s2", folder / "Show - s02e01.mkv")
        conn.execute("UPDATE media_items SET tmdb_id = 11, tvdb_id = 22 WHERE id = 's2'")
        conn.commit()

        DeletionExecutor(conn=conn).execute()

        audit_id = conn.execute(
            "SELECT id FROM audit_log WHERE media_item_id='s2' AND action='deleted'"
        ).fetchone()["id"]
        assert [tuple(r) for r in self._deleted_media(conn)] == [
            (audit_id, "season", 11, 22, 2, "Show")
        ]

    def test_recovery_with_only_artwork_left_books_the_item_deleted(
        self, conn, tmp_path, monkeypatch
    ):
        root = self._root(tmp_path, monkeypatch)
        folder = root / "Show" / "Season 1"
        folder.mkdir(parents=True)
        (folder / "season01-poster.jpg").write_bytes(b"p")
        insert_media_item(
            conn,
            id="s1",
            title="Show",
            plex_rating_key="s1",
            media_type="season",
            season_number=1,
            file_path=str(folder),
        )
        conn.execute(
            "UPDATE media_items SET tracked_files = ?, tvdb_id = 5 WHERE id = 's1'",
            (json.dumps([{"path": str(folder / "Show - s01e01.mkv"), "size": 300}]),),
        )
        insert_scheduled_action(
            conn,
            media_item_id="s1",
            action="scheduled_deletion",
            token="tok-s1",
            execute_at="2020-01-01",
            token_used=False,
            delete_status="deleting",
        )

        recover_stuck_deletions(conn)

        assert self._status(conn, "s1") is None
        audit = conn.execute(
            "SELECT id, action, space_reclaimed_bytes FROM audit_log WHERE media_item_id='s1'"
        ).fetchone()
        assert (audit["action"], audit["space_reclaimed_bytes"]) == ("deleted", 300)
        assert [tuple(r) for r in self._deleted_media(conn)] == [
            (audit["id"], "season", None, 5, 1, "Show")
        ]

    def test_dry_run_touches_nothing(self, conn, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch, "movies")
        folder = root / "Movie"
        folder.mkdir()
        (folder / "Movie.mkv").write_bytes(b"x")
        (folder / "Movie.en.srt").write_bytes(b"x")
        _insert_media(conn, file_path=str(folder / "Movie.mkv"))
        _insert_pending_deletion(conn)

        result = DeletionExecutor(conn=conn, dry_run=True).execute()

        assert result == {"deleted": 0, "reclaimed_bytes": 0}
        assert sorted(p.name for p in folder.iterdir()) == ["Movie.en.srt", "Movie.mkv"]
        assert self._status(conn, "m1") == ("scheduled_deletion", "pending")
        assert _audits(conn, "m1") == ["dry_run_skip"]
        assert self._deleted_media(conn) == []
