import pytest

from mediaman.services.infra.storage import (
    delete_path,
    get_aggregate_disk_usage,
    get_directory_size,
)


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
        """When all subdirs are on the same device, result has expected keys."""
        import shutil

        (tmp_path / "subdir").mkdir()
        single = shutil.disk_usage(str(tmp_path))
        agg = get_aggregate_disk_usage(str(tmp_path))
        assert agg["total_bytes"] == single.total
        assert agg["used_bytes"] == single.used

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
        # ``/media`` is a forbidden root — caught at allowlist validation.
        with pytest.raises(ValueError, match="system / mount-root"):
            delete_path("/", allowed_roots=["/media"])

    def test_refuses_etc_path(self):
        # ``/media`` is a forbidden root — caught at allowlist validation.
        with pytest.raises(ValueError, match="system / mount-root"):
            delete_path("/etc/passwd", allowed_roots=["/media"])

    def test_allows_path_under_allowed_root(self, tmp_path):
        target = tmp_path / "movie"
        target.mkdir()
        (target / "file.mkv").write_text("data")
        delete_path(str(target), allowed_roots=[str(tmp_path)])
        assert not target.exists()

    def test_refuses_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="strict descendant"):
            delete_path(str(tmp_path / ".." / "etc"), allowed_roots=[str(tmp_path)])

    def test_nonexistent_path_under_allowed_root(self, tmp_path):
        """Non-existent path under an allowed root should silently return."""
        delete_path(str(tmp_path / "does-not-exist"), allowed_roots=[str(tmp_path)])

    def test_refuses_prefix_collision(self, tmp_path):
        """A path like /media-backup must not pass when root is /media."""
        sibling = tmp_path / "media-backup"
        sibling.mkdir()
        with pytest.raises(ValueError, match="strict descendant"):
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


class TestDeletePathStrictDescendant:
    """Regression tests for the strict-descendant rule.

    A target *equal* to a configured root must always be refused — the
    old containment check ``p == root or root in p.parents`` would have
    let a crafted Plex part-path of ``/media`` recursively wipe the
    entire mount. These tests pin the new behaviour.
    """

    def test_refuses_target_equals_root(self, tmp_path):
        """Target equal to a configured root must never be deleted."""
        root = tmp_path / "library"
        root.mkdir()
        (root / "important.mkv").write_text("keep")
        with pytest.raises(ValueError, match="strict descendant"):
            delete_path(str(root), allowed_roots=[str(root)])
        # Root and its contents survive.
        assert root.exists()
        assert (root / "important.mkv").exists()

    def test_refuses_target_equals_resolved_root(self, tmp_path):
        """Target equal to the *resolved* form of a configured root is refused.

        Configures the root via a path that resolves to a different
        absolute string (e.g. ``./library`` or ``library/.``) and proves
        the equality check uses the resolved form, not the raw string.
        """
        root = tmp_path / "library"
        root.mkdir()
        (root / "movie.mkv").write_text("keep")
        # Pass the same logical root via a `.` indirection — both
        # forms must be rejected as equal-to-root.
        configured_root = root / "."
        with pytest.raises(ValueError, match="strict descendant"):
            delete_path(str(root), allowed_roots=[str(configured_root)])
        assert root.exists()

    def test_refuses_when_allowed_roots_contains_filesystem_root(self):
        """``/`` as a delete root is a configuration disaster — refuse."""
        with pytest.raises(ValueError, match="system / mount-root"):
            # Even an obviously-non-existent path must be refused before
            # the OS gets near it.
            delete_path("/something/inside", allowed_roots=["/"])

    def test_refuses_when_allowed_roots_contains_data(self):
        """``/data`` as a delete root is forbidden — refuse before any IO.

        ``/data`` is the conventional in-container app home; deletion at
        that level would wipe the database, sessions, and configuration.
        """
        with pytest.raises(ValueError, match="system / mount-root"):
            delete_path("/data/db.sqlite", allowed_roots=["/data"])

    def test_refuses_when_allowed_roots_contains_usr(self):
        """``/usr`` as a delete root is forbidden — refuse before any IO."""
        with pytest.raises(ValueError, match="system / mount-root"):
            delete_path("/usr/bin/python", allowed_roots=["/usr"])

    def test_refuses_when_allowed_roots_contains_etc(self):
        """``/etc`` as a delete root is rejected.

        On most Linux deployments ``/etc`` is a real directory and will
        be caught by the forbidden-root list. On macOS it's a symlink to
        ``/private/etc`` and is caught by the symlink-root check. Either
        message is an acceptable fail-closed response.
        """
        with pytest.raises(ValueError, match="system / mount-root|symlink"):
            delete_path("/etc/passwd", allowed_roots=["/etc"])

    def test_refuses_when_allowed_roots_contains_var(self):
        """``/var`` as a delete root is rejected (real-dir or symlink)."""
        with pytest.raises(ValueError, match="system / mount-root|symlink"):
            delete_path("/var/log/syslog", allowed_roots=["/var"])

    def test_refuses_relative_root(self):
        """A relative path in allowed_roots is a configuration error."""
        with pytest.raises(ValueError, match="absolute path"):
            delete_path("relative/path", allowed_roots=["relative"])

    def test_refuses_root_traversal_in_config(self):
        """``/data/../`` style root must be caught after resolution.

        The forbidden-root check operates on the **resolved** form, so
        ``/data/../`` (which resolves to ``/``) is still refused even
        though the literal string isn't in the forbidden set.
        """
        # Construct a token that resolves to a forbidden path.
        with pytest.raises(ValueError, match="system / mount-root|symlink"):
            delete_path("/should/not/run", allowed_roots=["/data/.."])

    def test_refuses_symlink_root_at_validation(self, tmp_path):
        """A symlinked allowed-root entry must be caught at validation, before IO."""
        real_root = tmp_path / "real"
        real_root.mkdir()
        (real_root / "f.txt").write_text("data")
        link_root = tmp_path / "link"
        link_root.symlink_to(real_root)
        with pytest.raises(ValueError, match="symlink"):
            delete_path(str(link_root / "f.txt"), allowed_roots=[str(link_root)])
        # Real root still intact.
        assert real_root.exists()
        assert (real_root / "f.txt").exists()

    def test_refuses_shallow_mount_point_target(self, tmp_path):
        """Even a *shallow* target equal to root must be refused.

        The Plex scanner can produce ``part.file = "/media"`` from a
        bare-mount library; the cleanup loop must refuse to treat that
        as a deletable path even though the path technically resides
        under itself.
        """
        # ``tmp_path`` is the root and the target — must refuse.
        target_dir = tmp_path / "movies"
        target_dir.mkdir()
        (target_dir / "1.mkv").write_text("x")
        with pytest.raises(ValueError, match="strict descendant"):
            delete_path(str(tmp_path), allowed_roots=[str(tmp_path)])
        # Mount-root contents survive.
        assert target_dir.exists()
        assert (target_dir / "1.mkv").exists()

    def test_allows_strict_descendant(self, tmp_path):
        """Sanity check: a strict descendant deletion still works."""
        root = tmp_path / "library"
        root.mkdir()
        target = root / "movie"
        target.mkdir()
        (target / "f.mkv").write_text("data")
        delete_path(str(target), allowed_roots=[str(root)])
        assert not target.exists()
        assert root.exists()

    def test_refuses_relative_target_path(self, tmp_path):
        """Target paths must be absolute — relative paths anchor on CWD."""
        with pytest.raises(ValueError, match="absolute"):
            delete_path("relative/target", allowed_roots=[str(tmp_path)])

    def test_picks_longest_matching_root(self, tmp_path):
        """When two roots share a parent, the most-specific one wins.

        With ``allowed_roots = ["/media", "/media/movies"]`` and a target
        ``/media/movies/film.mkv``, the longer ``/media/movies`` is the
        correct anchor for the device check. Picking the shorter
        ``/media`` would compare the target's device against the
        umbrella mount rather than the actual content mount and refuse
        as cross-device.
        """
        outer = tmp_path / "media"
        outer.mkdir()
        inner = outer / "movies"
        inner.mkdir()
        target = inner / "film.mkv"
        target.write_text("data")

        # Both orderings must work — picking the more general parent is
        # the bug; picking the most-specific descendant is the fix.
        delete_path(str(target), allowed_roots=[str(outer), str(inner)])
        assert not target.exists()

        # Same again with the inner mounted first.
        target.write_text("data")
        delete_path(str(target), allowed_roots=[str(inner), str(outer)])
        assert not target.exists()


class TestForbiddenRootsMacOS:
    """macOS ``/tmp``, ``/var``, ``/etc`` resolve to ``/private/*``.

    Without explicit ``/private/*`` entries in the forbidden list, an
    operator who mis-configures their delete root to ``/tmp`` on macOS
    would have it resolved to ``/private/tmp`` and slip past the
    bare-name forbidden check.
    """

    @pytest.mark.parametrize("root", ["/private", "/private/tmp", "/private/var", "/private/etc"])
    def test_private_subtrees_refused(self, root):
        with pytest.raises(ValueError, match="system / mount-root"):
            delete_path(f"{root}/somefile", allowed_roots=[root])


class TestGetDirectorySize:
    def test_calculates_total_size(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        (tmp_path / "b.txt").write_bytes(b"y" * 200)
        size = get_directory_size(str(tmp_path))
        assert size == 300
