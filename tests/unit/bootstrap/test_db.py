"""Tests for the data-dir writability preflight in bootstrap_db."""

from __future__ import annotations

import asyncio
import errno
import os
import re
from unittest.mock import patch

import pytest

import mediaman.bootstrap.data_dir as bootstrap_data_dir_mod
from mediaman.bootstrap.data_dir import (
    DataDirNotWritableError,
    assert_data_dir_writable,
    remediation_for,
)


def test_writable_dir_passes_silently_and_leaves_no_probe(tmp_path):
    """Happy path returns None and the probe self-cleans on success."""
    assert_data_dir_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_readonly_dir_raises_actionable_error(tmp_path):
    """A real read-only dir raises with a chown hint and the dir path."""
    if os.geteuid() == 0:
        pytest.skip("read-only dir is still writable by root")
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(DataDirNotWritableError) as excinfo:
            assert_data_dir_writable(tmp_path)
    finally:
        tmp_path.chmod(0o700)
    msg = str(excinfo.value)
    assert "is not writable" in msg
    assert re.search(r"chown -R \d+:\d+", msg), msg
    assert str(tmp_path) in msg


@pytest.mark.parametrize(
    ("err_no", "expected_substring"),
    [
        (errno.ENOSPC, "disk is full"),
        (errno.EROFS, "read-only"),
        (errno.EDQUOT, "quota"),
        (errno.EACCES, "wrong ownership"),
        (errno.EPERM, "wrong ownership"),
    ],
)
def test_errno_aware_remediation(tmp_path, err_no, expected_substring):
    """Each errno produces remediation prose that matches its actual cause."""

    def _raise(*_args, **_kwargs):
        raise OSError(err_no, os.strerror(err_no))

    with (
        patch.object(bootstrap_data_dir_mod.tempfile, "NamedTemporaryFile", _raise),
        pytest.raises(DataDirNotWritableError) as excinfo,
    ):
        assert_data_dir_writable(tmp_path)
    assert expected_substring in str(excinfo.value)


def test_unknown_errno_falls_back_to_chown_hint():
    """Errnos we don't enumerate still surface the most-likely-cause hint."""
    exc = OSError(errno.EIO, "I/O error")
    advice = remediation_for(exc)
    assert "chown -R" in advice
    assert "errno=" in advice


def test_bootstrap_db_uses_pathlib_join_for_db_path(tmp_path, monkeypatch):
    """``Path / 'mediaman.db'`` joining.

    A trailing slash on ``MEDIAMAN_DATA_DIR`` previously produced
    ``//mediaman.db`` because of the f-string concatenation. Path
    division squashes that.
    """
    from dataclasses import dataclass

    from mediaman.bootstrap.db import bootstrap_db

    @dataclass
    class _Config:
        data_dir: str = ""
        secret_key: str = "x"

    class _State:
        pass

    class _App:
        state = _State()

    cfg = _Config(data_dir=str(tmp_path) + "/")  # trailing slash
    app = _App()
    bootstrap_db(app, cfg)

    expected = str(tmp_path / "mediaman.db")
    assert app.state.db_path == expected
    assert "//" not in app.state.db_path
    app.state.db.close()


def test_bootstrap_db_mkdir_failure_raises_data_dir_not_writable(monkeypatch, tmp_path):
    """A mkdir error surfaces as DataDirNotWritableError, not OSError."""
    from dataclasses import dataclass
    from pathlib import Path

    from mediaman.bootstrap.db import DataDirNotWritableError, bootstrap_db

    @dataclass
    class _Config:
        data_dir: str = str(tmp_path / "child")
        secret_key: str = "x"

    class _State:
        pass

    class _App:
        state = _State()

    def boom(self, *_a, **_kw):
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(DataDirNotWritableError) as excinfo:
        bootstrap_db(_App(), _Config())
    msg = str(excinfo.value)
    assert "could not be created" in msg
    assert "chown" in msg


def test_lifespan_logs_an_old_database_without_a_traceback_and_exits(monkeypatch, tmp_path, caplog):
    """The refusal is one CRITICAL line, not a traceback, and startup stops."""
    import logging
    import sqlite3

    from fastapi import FastAPI

    from mediaman.app_factory import lifespan

    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "mediaman.db")
    conn.execute("PRAGMA user_version=35")
    conn.close()

    async def start():
        async with lifespan(FastAPI()):
            pass

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as excinfo:
        asyncio.run(start())

    assert excinfo.value.code == 1
    [record] = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert "schema version 35" in record.getMessage()
    assert record.exc_info is None
