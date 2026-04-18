import os
import pytest
from mediaman.services.storage import get_disk_usage, get_aggregate_disk_usage, delete_path, get_directory_size

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


class TestGetDirectorySize:
    def test_calculates_total_size(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        (tmp_path / "b.txt").write_bytes(b"y" * 200)
        size = get_directory_size(str(tmp_path))
        assert size == 300
