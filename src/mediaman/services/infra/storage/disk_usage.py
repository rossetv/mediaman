"""Disk-usage and directory-size queries — the read-only side of storage.

This module owns the non-destructive filesystem queries:
``get_aggregate_disk_usage`` (multi-device usage roll-up) and
``get_directory_size`` (recursive byte count). The destructive
deletion operations live in ``deletion``; the delete-root allowlist
validation lives in ``_delete_roots``.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat as _stat
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


# 0.1% of a multi-terabyte drive is gigabytes, enough to fold two real drives into one.
_FREE_TOLERANCE_CAP = 256 * 1024 * 1024


def _accumulate_unique_disks(paths: Iterable[str]) -> dict[str, int]:
    """Sum ``shutil.disk_usage`` across *paths*, counting each disk once.

    Two paths are one disk when their totals are equal and their free bytes
    differ by under 0.1% of the total, at most 256 MiB.  The device id is left out of the
    comparison on purpose: one filesystem can show several ids (btrfs
    subvolumes, NFS mounts), and container binds can put two drives behind a
    single id.  The free bytes get a tolerance because they move between two
    reads of the same live disk, while two equal-sized drives rarely hold the
    same free space.  The first reading of each disk is the one summed.

    Non-existent or unreadable paths are skipped.
    """
    seen: list[tuple[int, int]] = []
    total = used = free = 0
    for p in paths:
        if not p or not Path(p).exists():
            continue
        try:
            usage = shutil.disk_usage(p)
        except OSError:
            logger.debug("disk_usage failed for %s — skipping", p)
            continue
        if any(
            usage.total == total_seen
            and abs(usage.free - free_seen) <= min(usage.total * 0.001, _FREE_TOLERANCE_CAP)
            for total_seen, free_seen in seen
        ):
            continue
        seen.append((usage.total, usage.free))
        total += usage.total
        used += usage.used
        free += usage.free
    return {"total_bytes": total, "used_bytes": used, "free_bytes": free}


def get_disk_usage_for_paths(paths: Iterable[str]) -> dict[str, int]:
    """Return combined disk usage across an explicit list of *paths*.

    Each underlying physical disk is counted once (see
    ``_accumulate_unique_disks``).  Use this when the media libraries
    live on a known set of mount points (e.g. the configured per-library
    paths) rather than under a single base directory.
    """
    return _accumulate_unique_disks(paths)


def get_aggregate_disk_usage(base_path: str) -> dict[str, int]:
    """Return combined disk usage across all unique disks under *base_path*.

    Detects subdirectories on different devices (e.g. a separate disk mounted
    at ``/media/movies`` under ``/media``) and sums their usage so the total
    reflects all physical disks, not just the root mount.
    """
    if not Path(base_path).exists():
        raise FileNotFoundError(f"Path does not exist: {base_path}")

    paths = [base_path]
    try:
        paths.extend(
            entry.path for entry in os.scandir(base_path) if entry.is_dir(follow_symlinks=False)
        )
    except PermissionError:
        logger.debug("Cannot scandir %s — skipping subdirectory enumeration", base_path)

    return _accumulate_unique_disks(paths)


def get_directory_size(path: str) -> int:
    """Return the total size in bytes of all regular files under *path*.

    Walks with ``os.walk(..., followlinks=False)`` so directory symlinks are
    never followed: a symlink cycle (``a/b -> a``) would otherwise loop until
    the path-length limit, and a symlink to a sibling tree would double-count
    its files. Symlinked *files* are skipped (``Path.is_file`` follows the
    link, but a broken or out-of-tree link should not contribute its target's
    size to this tree); only regular files are counted, via ``os.lstat`` so
    the stat itself never follows a link.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if _stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total
