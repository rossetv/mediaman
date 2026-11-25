"""Uploaded photos on disk, in ``<data_dir>/avatars`` beside the database, under random names."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Collection
from pathlib import Path

logger = logging.getLogger(__name__)

# A younger file may be one whose row is still being written.
_ORPHAN_AGE_SECONDS = 3600
_NAME_RE = re.compile(r"[0-9a-f]{32}")


def folder(conn: sqlite3.Connection) -> Path:
    """The folder of the install *conn* is open on."""
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path).parent / "avatars"
    raise RuntimeError("avatars need a database kept in a file")


def write(where: Path, data: bytes) -> str:
    """Keep *data* in *where* and return its random name; the file appears whole or not at all."""
    where.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = secrets.token_hex(16)
    part = where / f"{name}.part"
    try:
        with part.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, where / name)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    return name


def read(where: Path, name: str) -> bytes | None:
    """The bytes of the photo *name*, or ``None`` when they cannot be read."""
    if not _NAME_RE.fullmatch(name):
        return None
    try:
        return (where / name).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("could not read an avatar", exc_info=True)
        return None


def remove(where: Path, name: str | None) -> None:
    """Remove the photo *name*, if any; one that cannot be removed now is left to `sweep`."""
    if name is None or not _NAME_RE.fullmatch(name):
        return
    try:
        (where / name).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove an avatar", exc_info=True)


def sweep(where: Path, kept: Collection[str]) -> None:
    """Remove the files no row names, such as those a crash left; never one written just now."""
    before = time.time() - _ORPHAN_AGE_SECONDS
    try:
        for path in where.iterdir():
            if not _NAME_RE.fullmatch(path.name.removesuffix(".part")) or path.name in kept:
                continue
            if path.stat().st_mtime < before:
                path.unlink(missing_ok=True)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("could not sweep avatars", exc_info=True)
