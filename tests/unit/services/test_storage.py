import pytest

from mediaman.services.infra.storage import (
    delete_path,
    get_aggregate_disk_usage,
    get_directory_size,
    get_disk_usage,
)


class TestDiskUsage:
    def test_returns_usage_dict(self, tmp_path):
        usage = get_disk_usage(str(tmp_path))
        assert "total_bytes" in usage
        assert "used_bytes" in usage
        assert "free_bytes" in usage
        assert usage["total_bytes"] > 0

    def test_nonexistent_path_raises(self):
        with pytest.raises(FileNotFoundError):
            get_disk_usage("/nonexistent/path/12345")


class TestDeletePath:
    def test_deletes_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        assert f.exists()
        delete_path(str(f), allowed_roots=[str(tmp_path)])
        assert not f.exists()

    def test_deletes_directory(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        (d / "file.txt").write_text("content")
        delete_path(str(d), allowed_roots=[str(tmp_path)])
        assert not d.exists()

    def test_nonexistent_path_is_noop(self, tmp_path):
        delete_path(str(tmp_path / "nope"), allowed_roots=[str(tmp_path)])


class TestAggregateDiskUsage:
    def test_returns_usage_for_single_device(self, tmp_path):
        """When all subdirs are on the same device, result matches get_disk_usage."""
        (tmp_path / "subdir").mkdir()
        single = get_disk_usage(str(tmp_path))
        agg = get_aggregate_disk_usage(str(tmp_path))
        assert agg["total_bytes"] == single["total_bytes"]
        assert agg["used_bytes"] == single["used_bytes"]

    def test_nonexistent_path_raises(self):
        with pytest.raises(FileNotFoundError):
            get_aggregate_disk_usage("/nonexistent/path/12345")

    def test_includes_subdirectories(self, tmp_path):
        """Subdirs on the same device don't double-count."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        agg = get_aggregate_disk_usage(str(tmp_path))
        assert agg["total_bytes"] > 0
        assert agg["free_bytes"] > 0


class TestDeletePathValidation:
    def test_refuses_root_path(self):
        with pytest.raises(ValueError, match="outside allowed"):
            delete_path("/", allowed_roots=["/media"])

    def test_refuses_etc_path(self):
        with pytest.raises(ValueError, match="outside allowed"):
            delete_path("/etc/passwd", allowed_roots=["/media"])

    def test_allows_path_under_allowed_root(self, tmp_path):
        target = tmp_path / "movie"
        target.mkdir()
        (target / "file.mkv").write_text("data")
        delete_path(str(target), allowed_roots=[str(tmp_path)])
        assert not target.exists()

    def test_refuses_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="outside allowed"):
            delete_path(str(tmp_path / ".." / "etc"), allowed_roots=[str(tmp_path)])

    def test_nonexistent_path_under_allowed_root(self, tmp_path):
        """Non-existent path under an allowed root should silently return."""
        delete_path(str(tmp_path / "does-not-exist"), allowed_roots=[str(tmp_path)])

    def test_refuses_prefix_collision(self, tmp_path):
        """A path like /media-backup must not pass when root is /media."""
        sibling = tmp_path / "media-backup"
        sibling.mkdir()
        with pytest.raises(ValueError, match="outside allowed"):
            delete_path(str(sibling), allowed_roots=[str(tmp_path / "media")])

    def test_rejects_missing_allowed_roots(self, tmp_path):
        """Calling without allowed_roots must raise — prevents accidental bypass."""
        target = tmp_path / "anywhere"
        target.mkdir()
        with pytest.raises(ValueError, match="requires allowed_roots"):
            delete_path(str(target))

    def test_empty_allowed_roots_refuses_deletion(self, tmp_path):
        """An empty allowlist must fail closed — no deletion ever happens."""
        target = tmp_path / "anywhere"
        target.mkdir()
        (target / "f.txt").write_text("data")
        with pytest.raises(ValueError, match="not configured"):
            delete_path(str(target), allowed_roots=[])
        # File system untouched.
        assert target.exists()


class TestDeletePathSymlinkSafety:
    """Regression tests for the TOCTOU / symlink hardening in delete_path.

    Each case builds a crafted layout and proves _safe_rmtree refuses to
    escape the allowed root.
    """

    def test_refuses_symlink_target_pointing_outside_root(self, tmp_path):
        """A symlink passed as the target (resolving inside the root) must
        still be refused — we never want to delete *through* a symlink."""
        root = tmp_path / "media"
        root.mkdir()
        real = root / "real"
        real.mkdir()
        (real / "f.txt").write_text("data")
        link = root / "link"
        link.symlink_to(real)

        with pytest.raises(ValueError, match="symlink"):
            delete_path(str(link), allowed_roots=[str(root)])
        # Real target untouched.
        assert real.exists()
        assert (real / "f.txt").exists()

    def test_refuses_symlink_discovered_mid_walk(self, tmp_path):
        """A nested symlink swapped in under the target directory must
        not cause the walker to follow it and delete outside the root."""
        root = tmp_path / "media"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "important.txt").write_text("keep me")

        target = root / "todelete"
        target.mkdir()
        (target / "a.txt").write_text("junk")
        (target / "escape").symlink_to(outside)

        delete_path(str(target), allowed_roots=[str(root)])

        # The deletion target is gone.
        assert not target.exists()
        # The outside tree is untouched — only the link entry disappeared.
        assert outside.exists()
        assert (outside / "important.txt").exists()

    def test_refuses_root_that_is_itself_a_symlink(self, tmp_path):
        """A delete_allowed_roots entry that is a symlink is a
        misconfiguration and must cause deletion to be refused."""
        real_root = tmp_path / "real_root"
        real_root.mkdir()
        (real_root / "f.txt").write_text("data")
        link_root = tmp_path / "link_root"
        link_root.symlink_to(real_root)

        target = real_root / "sub"
        target.mkdir()
        (target / "inner.txt").write_text("x")

        # Root is the symlink, target is inside its resolved content.
        with pytest.raises(ValueError, match="symlink"):
            delete_path(str(target), allowed_roots=[str(link_root)])
        # Target is untouched.
        assert target.exists()

    def test_allows_valid_nested_tree_inside_root(self, tmp_path):
        """Happy path: a regular nested directory tree inside the root
        is fully removed."""
        root = tmp_path / "media"
        root.mkdir()
        target = root / "show" / "season 1"
        target.mkdir(parents=True)
        (target / "s01e01.mkv").write_bytes(b"x" * 100)
        (target / "s01e02.mkv").write_bytes(b"x" * 100)
        nested = target / "subs"
        nested.mkdir()
        (nested / "en.srt").write_text("subs")

        delete_path(str(target), allowed_roots=[str(root)])

        assert not target.exists()
        # Parent still there — we only removed the target.
        assert (root / "show").exists()


class TestGetDirectorySize:
    def test_calculates_total_size(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        (tmp_path / "b.txt").write_bytes(b"y" * 200)
        size = get_directory_size(str(tmp_path))
        assert size == 300
