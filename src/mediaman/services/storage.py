"""Filesystem operations — disk usage, deletion, size calculation."""

import os
import shutil
from pathlib import Path


def get_disk_usage(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Path does not exist: {path}")
    usage = shutil.disk_usage(path)
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}


def get_aggregate_disk_usage(base_path: str) -> dict:
    """Return combined disk usage across all unique mount points under *base_path*.

    Detects subdirectories on different devices (e.g. a separate disk mounted
    at ``/media/movies`` under ``/media``) and sums their usage so the total
    reflects all physical disks, not just the root mount.
    """
    if not os.path.exists(base_path):
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
    p = Path(path).resolve()
    resolved_roots = [Path(r).resolve() for r in allowed_roots]
    if not any(p == root or root in p.parents for root in resolved_roots):
        raise ValueError(
            f"Refusing to delete '{p}' — outside allowed roots: "
            f"{[str(r) for r in resolved_roots]}"
        )
    if not p.exists():
        return
    if p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def get_directory_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total
