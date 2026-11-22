"""Tests for mediaman.scanner.deletions.

Covers: DeletionExecutor.execute (dry_run, no allowed roots, actual delete
path, stuck-state recovery) and recover_stuck_deletions.
"""

from __future__ import annotations

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


def _insert_media(conn, *, media_id="m1", title="Test Film", file_path="/tmp/test.mkv"):
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        plex_rating_key=media_id,
        file_path=file_path,
        file_size_bytes=1000000,
    )


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

        # We stored file_size_bytes=1000000 in the DB row.
        assert result["reclaimed_bytes"] == 1_000_000

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

    def _season(self, conn, media_id, path, number, size=10):
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

        assert not show.exists()
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
        assert result == {"deleted": 2, "reclaimed_bytes": 30}
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
        self._season(conn, "s1", season_one, 1, size=10)
        self._season(conn, "s2", show, 2, size=20)
        for media_id in (first, "s1" if first == "s2" else "s2"):
            _due_row(conn, media_id)

        result = DeletionExecutor(conn=conn).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 30}
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
        self._season(conn, "s1", f"{root}//Show/Season 1", 1, size=10)
        self._season(conn, "s2", show, 2, size=20)
        for media_id in (first, "s1" if first == "s2" else "s2"):
            _due_row(conn, media_id)

        result = DeletionExecutor(conn=conn).execute()

        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 30}
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
        real_delete = deletions.delete_path

        def keep_then_delete(path, *, allowed_roots):
            # Timed at s2's own deadline so only the pass's claim can refuse it.
            keep_time = datetime.fromisoformat(s2["execute_at"])
            with other:
                kept.append(apply_keep_forever(other, s2["id"], keep_time))
            real_delete(path, allowed_roots=allowed_roots)

        monkeypatch.setattr(deletions, "delete_path", keep_then_delete)

        result = DeletionExecutor(conn=conn).execute()

        assert kept == [0]
        assert not show.exists()
        assert result == {"deleted": 2, "reclaimed_bytes": 20}
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

        def refuse(path, *, allowed_roots):
            raise DeletionRefused("refused")

        monkeypatch.setattr(deletions, "delete_path", refuse)

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

    def test_file_gone_while_mount_is_up_closes_the_row(self, conn, tmp_path, monkeypatch):
        root = tmp_path / "movies"
        (root / "Other").mkdir(parents=True)
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"))
        _due_row(conn, "m")

        result = DeletionExecutor(conn=conn).execute()

        assert result["deleted"] == 1
        assert _actions(conn, "m") == []
        assert _audits(conn, "m") == ["deleted"]
        assert (root / "Other" / "Other.mkv").exists()


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
        assert result == {"deleted": 2, "reclaimed_bytes": 20}
        assert _actions(conn, "s2") == []


class TestDiskChecksOutsideWriteLock:
    """A hung media mount must not hold the database write lock."""

    def _record_transaction_state(self, monkeypatch, conn, seen):
        import os

        from mediaman.scanner import deletions

        real_lexists = os.path.lexists
        real_root_available = deletions.delete_root_available

        def lexists(path):
            seen.append(conn.in_transaction)
            return real_lexists(path)

        def root_available(path, roots):
            seen.append(conn.in_transaction)
            return real_root_available(path, roots)

        monkeypatch.setattr(os.path, "lexists", lexists)
        monkeypatch.setattr(deletions, "delete_root_available", root_available)

    def test_pass_checks_a_missing_target_with_no_transaction_open(
        self, conn, tmp_path, monkeypatch
    ):
        root = tmp_path / "movies"
        (root / "Other").mkdir(parents=True)
        (root / "Other" / "Other.mkv").write_bytes(b"x")
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        insert_media_item(conn, id="m", plex_rating_key="m", file_path=str(root / "M" / "M.mkv"))
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
        _due_row(conn, "m1")
        return folder

    def _row(self, conn):
        row = conn.execute(
            "SELECT action, delete_status FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchone()
        return None if row is None else (row["action"], row["delete_status"])

    def _failing_pass(self, conn, monkeypatch, error, during=None):
        calls = []

        def fail(path, *, allowed_roots):
            calls.append(path)
            if during is not None:
                during()
            raise error

        with monkeypatch.context() as patch:
            patch.setattr("mediaman.scanner.deletions.delete_path", fail)
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
        real = deletions.delete_path
        seen = {}

        def remove(path, *, allowed_roots):
            keep(other, "m1")
            seen["row"] = self._row(other)
            real(path, allowed_roots=allowed_roots)

        monkeypatch.setattr(deletions, "delete_path", remove)

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
        """Run *change* the first time the pass checks *path* while *media_id* is claimed."""
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
        _due_row(conn, "m1")
        return folder

    @pytest.mark.parametrize("keep", [_keep_forever, _keep_show], ids=["library", "show"])
    def test_keep_after_the_claim_leaves_the_media_and_keeps_the_row(
        self, conn, db_path, tmp_path, monkeypatch, keep
    ):
        folder = self._movie(conn, tmp_path, monkeypatch)
        other = init_db(str(db_path))
        done = self._change_once_claimed(monkeypatch, other, folder, "m1", keep)
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
        done = self._change_once_claimed(monkeypatch, other, folder, "m1", _clear_scheduled)

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
            _due_row(conn, media_id)
        other = init_db(str(db_path))
        done = self._change_once_claimed(monkeypatch, other, show, "s2", _keep_forever)
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
        done = self._change_once_claimed(monkeypatch, other, folder, "s2", self._keep_whole_show)
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
        done = self._change_once_claimed(monkeypatch, other, show, "s2", self._keep_whole_show)

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
