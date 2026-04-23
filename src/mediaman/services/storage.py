"""Filesystem operations — disk usage, deletion, size calculation."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("mediaman")


def get_disk_usage(path: str) -> dict[str, int]:
    """Return disk usage for *path*. Raises :exc:`FileNotFoundError` if the path does not exist."""
    if not Path(path).exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    usage = shutil.disk_usage(path)
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}


def get_aggregate_disk_usage(base_path: str) -> dict[str, int]:
    """Return combined disk usage across all unique mount points under *base_path*.

    Detects subdirectories on different devices (e.g. a separate disk mounted
    at ``/media/movies`` under ``/media``) and sums their usage so the total
    reflects all physical disks, not just the root mount.
    """
    if not Path(base_path).exists():
        raise FileNotFoundError(f"Path does not exist: {base_path}")

    seen_devices: set[int] = set()
    total = 0
    used = 0
    free = 0

    def _add_path(p: str) -> None:
        nonlocal total, used, free
        dev = os.stat(p).st_dev
        if dev in seen_devices:
            return
        seen_devices.add(dev)
        usage = shutil.disk_usage(p)
        total += usage.total
        used += usage.used
        free += usage.free

    _add_path(base_path)
    try:
        for entry in os.scandir(base_path):
            if entry.is_dir(follow_symlinks=False):
                _add_path(entry.path)
    except PermissionError:
        pass

    return {"total_bytes": total, "used_bytes": used, "free_bytes": free}


def delete_path(path: str, *, allowed_roots: list[str] | None = None) -> None:
    """Delete a file or directory, with mandatory path validation.

    ``allowed_roots`` is required and must be a non-empty list. Raises
    ``ValueError`` if it is missing or empty, or if the resolved path is
    not under one of the roots. This fail-closed guard prevents a
    compromised or misconfigured upstream (e.g. a Plex response
    containing a crafted file path) from triggering ``rmtree`` outside
    the media mounts.
    """
    if allowed_roots is None:
        raise ValueError(
            "delete_path requires allowed_roots — refusing deletion until "
            "a trusted allowlist is supplied."
        )
    if not allowed_roots:
        raise ValueError(
            "delete_allowed_roots not configured; refusing deletion. "
            "Set the delete_allowed_roots setting (JSON list) or the "
            "MEDIAMAN_DELETE_ROOTS env var (colon-separated)."
        )
    raw = Path(path)
    p = raw.resolve()
    resolved_roots = [Path(r).resolve() for r in allowed_roots]
    if not any(p == root or root in p.parents for root in resolved_roots):
        raise ValueError(
            f"Refusing to delete '{p}' — outside allowed roots: "
            f"{[str(r) for r in resolved_roots]}"
        )
    # Refuse if the caller's path itself is a symlink — resolving
    # follows it, so the containment check above was against the
    # target, not the link. A symlink passed as the deletion target is
    # always suspicious regardless of where it points.
    if raw.is_symlink():
        raise ValueError(
            f"Refusing to delete '{raw}' — target path is a symlink."
        )
    _safe_rmtree(p, resolved_roots, original_allowed_roots=allowed_roots)


def _safe_rmtree(
    path: Path,
    allowed_roots: list[Path],
    *,
    original_allowed_roots: list[str] | None = None,
) -> None:
    """Delete *path* without following symlinks or escaping *allowed_roots*.

    Hardens against TOCTOU / symlink-swap attacks between the caller's
    containment check and the actual removal:

    * Refuses to descend into any symlinked directory — target, an
      allowed root that is itself a symlink, or any nested entry found
      mid-walk.
    * Re-resolves *path* and re-checks containment at delete time,
      rather than trusting the caller's earlier resolve.
    * Stays on the same filesystem device as the resolved root so a
      mount swapped in mid-walk cannot redirect deletions.
    * Uses ``os.fwalk(..., follow_symlinks=False)`` so even if a
      directory is swapped for a symlink after our initial stat, the
      walk's file-descriptor-based rmdir/unlink refuses to cross it.

    Non-existent paths are a silent no-op (parity with the previous
    implementation). A single *file* target is unlinked after the same
    symlink / containment checks.
    """
    # Re-resolve now, don't trust an earlier resolve. Any component that
    # was a symlink at check time but a real directory now (or vice
    # versa) is caught here.
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError(
            f"Refusing to delete '{resolved}' — outside allowed roots on "
            f"re-check: {[str(r) for r in allowed_roots]}"
        )

    # An allowed root that is itself a symlink is treated as
    # misconfigured — refuse outright so no one can swap the root
    # symlink to repoint into /etc. Check the original (pre-resolve)
    # root strings because Path.resolve() follows the link.
    for raw_root in (original_allowed_roots or []):
        if Path(raw_root).is_symlink():
            raise ValueError(
                f"Refusing to delete: allowed root '{raw_root}' is a symlink. "
                "Configure delete_allowed_roots with real directories only."
            )

    # Missing: silent no-op (matches previous behaviour).
    try:
        lst = os.lstat(str(resolved))
    except FileNotFoundError:
        return

    # Target itself must not be a symlink — otherwise an attacker could
    # swap the intended directory for a link to / after the containment
    # check.
    if os.path.islink(str(resolved)):
        raise ValueError(
            f"Refusing to delete '{resolved}' — target is a symlink."
        )

    # Identify which root we're rooted under so we can pin to its device.
    pinned_root: Path | None = None
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            pinned_root = root
            break
    assert pinned_root is not None  # containment check above already verified
    try:
        root_dev = os.stat(str(pinned_root)).st_dev
    except FileNotFoundError as exc:
        raise ValueError(
            f"Refusing to delete '{resolved}' — allowed root '{pinned_root}' "
            "no longer exists."
        ) from exc

    # Must live on the same device as the root (defeats mount-swap).
    if lst.st_dev != root_dev:
        raise ValueError(
            f"Refusing to delete '{resolved}' — different device from "
            f"allowed root '{pinned_root}'."
        )

    import stat as _stat

    if _stat.S_ISREG(lst.st_mode):
        os.unlink(str(resolved))
        return
    if not _stat.S_ISDIR(lst.st_mode):
        raise ValueError(
            f"Refusing to delete '{resolved}' — not a regular file or directory."
        )

    # Walk bottom-up, never following symlinks. Every entry is checked
    # against the root device and must not be a symlink before we unlink
    # or rmdir it. ``os.fwalk`` gives us a dir fd per step so the rm
    # operations are relative to the opened directory, not re-resolved
    # from the original path — that's what defeats a symlink swap
    # between our lstat and our unlink.
    for dirpath, dirnames, filenames, dirfd in os.fwalk(
        str(resolved), topdown=False, follow_symlinks=False
    ):
        # Refuse to descend into any device other than the pinned root's.
        try:
            dev = os.fstat(dirfd).st_dev
        except OSError as exc:
            raise ValueError(
                f"Refusing to delete '{resolved}' — cannot stat '{dirpath}': {exc}"
            ) from exc
        if dev != root_dev:
            raise ValueError(
                f"Refusing to delete '{resolved}' — '{dirpath}' is on a "
                "different device than the allowed root."
            )

        for name in filenames:
            entry_stat = os.lstat(name, dir_fd=dirfd)
            if _stat.S_ISLNK(entry_stat.st_mode):
                # Remove symlink entries themselves (they don't follow
                # the link to delete its target) — this is safe and the
                # only way to empty the dir.
                os.unlink(name, dir_fd=dirfd)
                continue
            if entry_stat.st_dev != root_dev:
                raise ValueError(
                    f"Refusing to delete '{resolved}' — entry '{name}' in "
                    f"'{dirpath}' is on a different device."
                )
            os.unlink(name, dir_fd=dirfd)

        for name in dirnames:
            entry_stat = os.lstat(name, dir_fd=dirfd)
            if _stat.S_ISLNK(entry_stat.st_mode):
                # Symlinked subdir: remove the link entry, don't recurse
                # (fwalk already refuses with follow_symlinks=False).
                os.unlink(name, dir_fd=dirfd)
                continue
            if entry_stat.st_dev != root_dev:
                raise ValueError(
                    f"Refusing to delete '{resolved}' — subdirectory "
                    f"'{name}' in '{dirpath}' is on a different device."
                )
            os.rmdir(name, dir_fd=dirfd)

    # Finally remove the top-level directory itself.
    os.rmdir(str(resolved))


def get_directory_size(path: str) -> int:
    """Return the total size in bytes of all regular files under *path*."""
    total = 0
    for fp in Path(path).rglob("*"):
        if fp.is_file():
            total += fp.stat().st_size
    return total
