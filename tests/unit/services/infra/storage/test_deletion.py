"""Tests for the delete-root availability check."""

from __future__ import annotations

from mediaman.services.infra.storage import delete_root_available


def test_a_dropped_mount_below_the_root_is_unavailable(tmp_path):
    root = tmp_path / "media"
    (root / "tv" / "local" / "Show").mkdir(parents=True)
    (root / "tv" / "local" / "Show" / "e01.mkv").write_text("x")
    (root / "tv" / "nas").mkdir()

    assert not delete_root_available(str(root / "tv" / "nas" / "Show" / "e01.mkv"), [str(root)])


def test_a_missing_target_beside_other_media_is_available(tmp_path):
    root = tmp_path / "media"
    (root / "tv" / "nas" / "Other").mkdir(parents=True)

    assert delete_root_available(str(root / "tv" / "nas" / "Show" / "e01.mkv"), [str(root)])
