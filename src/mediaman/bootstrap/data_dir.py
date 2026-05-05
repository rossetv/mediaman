"""Data-directory writability checks used during application bootstrap."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path


class DataDirNotWritableError(RuntimeError):
    """Raised when the data directory cannot be written by the current process.

    The Dockerfile pins the runtime identity to uid/gid 1000:1000. If an
    operator bind-mounts a host directory whose ownership doesn't match,
    SQLite eventually fails mid-migration with an opaque "attempt to write
    a readonly database" stack trace. We probe writability up-front so the
    operator sees one actionable line instead of a Python traceback.
    """


def _remediation_for(exc: OSError) -> str:
    """Return errno-tailored remediation advice for an OSError on the data dir."""
    proc_uid = os.geteuid()
    proc_gid = os.getegid()
    if exc.errno == errno.ENOSPC:
        return "disk is full — free space on the host filesystem backing /data"
    if exc.errno == errno.EROFS:
        return "filesystem is mounted read-only — remount rw or use a different path"
    if exc.errno == errno.EDQUOT:
        return "disk quota exceeded for the owning user — raise quota or free space"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"likely wrong ownership — on the host run: "
            f"chown -R {proc_uid}:{proc_gid} <your-bind-mount-for-/data>"
        )
    return (
        f"unexpected error (errno={exc.errno}) — most often this is wrong "
        f"ownership; on the host try: "
        f"chown -R {proc_uid}:{proc_gid} <your-bind-mount-for-/data>"
    )


def _assert_data_dir_writable(data_dir: Path) -> None:
    """Fail fast and loud if ``data_dir`` is not writable by this process.

    Uses a self-cleaning temp file rather than a fixed probe path so a
    partial failure can't leave a stray file behind. ``os.access`` is not
    used because it consults real (not effective) uid and ignores read-only
    filesystem mounts and ACLs.
    """
    try:
        with tempfile.NamedTemporaryFile(
            dir=data_dir, prefix=".mediaman-write-probe-", delete=True
        ):
            pass
    except OSError as exc:
        proc_uid = os.geteuid()
        proc_gid = os.getegid()
        try:
            st = data_dir.stat()
            owner = f"uid={st.st_uid} gid={st.st_gid}"
        except OSError:
            owner = "uid=? gid=? (stat failed)"
        raise DataDirNotWritableError(
            f"data dir {data_dir} is not writable by uid={proc_uid} "
            f"gid={proc_gid} (currently owned by {owner}); "
            f"{_remediation_for(exc)}; underlying error: {exc}"
        ) from exc


__all__ = [
    "DataDirNotWritableError",
    "_assert_data_dir_writable",
    "_remediation_for",
]
