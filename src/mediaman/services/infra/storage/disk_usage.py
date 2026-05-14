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
from pathlib import Path

logger = logging.getLogger(__name__)


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
        logger.debug("Cannot scandir %s — skipping subdirectory enumeration", base_path)

    return {"total_bytes": total, "used_bytes": used, "free_bytes": free}


def get_directory_size(path: str) -> int:
    """Return the total size in bytes of all regular files under *path*."""
    total = 0
    for fp in Path(path).rglob("*"):
        if fp.is_file():
            total += fp.stat().st_size
    return total
