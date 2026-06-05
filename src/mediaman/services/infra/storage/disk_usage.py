"""Disk-usage and directory-size queries — the read-only side of storage.

This module owns the non-destructive filesystem queries:
:func:`get_aggregate_disk_usage` (multi-device usage roll-up) and
:func:`get_directory_size` (recursive byte count). The destructive
deletion operations live in :mod:`.deletion`; the delete-root allowlist
validation lives in :mod:`._delete_roots`.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def _accumulate_unique_disks(paths: Iterable[str]) -> dict[str, int]:
    """Sum ``shutil.disk_usage`` across *paths*, counting each disk once.

    Two paths on the same physical disk are de-duplicated by their
    ``(total, used, free)`` reading rather than by ``st_dev``.  ``st_dev``
    is unreliable across container bind mounts: OrbStack/Docker present
    every host bind under a single synthetic device id, so a device-keyed
    de-dup collapses genuinely-separate disks (e.g. a movies drive and a
    TV drive) into whichever path is scanned first — undercounting the
    total.  The byte-tuple is stable per filesystem and differs between
    real disks (their *used* byte counts essentially never coincide), so
    it both merges same-disk paths (``/media/tv`` + ``/media/anime`` on
    one drive) and keeps separate disks separate.

    Non-existent or unreadable paths are skipped.
    """
    seen: set[tuple[int, int, int]] = set()
    total = used = free = 0
    for p in paths:
        if not p or not Path(p).exists():
            continue
        try:
            usage = shutil.disk_usage(p)
        except OSError:
            logger.debug("disk_usage failed for %s — skipping", p)
            continue
        key = (usage.total, usage.used, usage.free)
        if key in seen:
            continue
        seen.add(key)
        total += usage.total
        used += usage.used
        free += usage.free
    return {"total_bytes": total, "used_bytes": used, "free_bytes": free}


def get_disk_usage_for_paths(paths: Iterable[str]) -> dict[str, int]:
    """Return combined disk usage across an explicit list of *paths*.

    Each underlying physical disk is counted once (see
    :func:`_accumulate_unique_disks`).  Use this when the media libraries
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
    """Return the total size in bytes of all regular files under *path*."""
    total = 0
    for fp in Path(path).rglob("*"):
        if fp.is_file():
            total += fp.stat().st_size
    return total
