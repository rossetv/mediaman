"""Tests for the data-dir writability preflight in bootstrap_db."""

from __future__ import annotations

import errno
import os
import re
from unittest.mock import patch

import pytest

from mediaman.bootstrap import db as bootstrap_db_mod
from mediaman.bootstrap.db import (
    DataDirNotWritableError,
    _assert_data_dir_writable,
    _remediation_for,
)


def test_writable_dir_passes_silently_and_leaves_no_probe(tmp_path):
    """Happy path returns None and the probe self-cleans on success."""
    _assert_data_dir_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_readonly_dir_raises_actionable_error(tmp_path):
    """A real read-only dir raises with a chown hint and the dir path."""
    if os.geteuid() == 0:
        pytest.skip("read-only dir is still writable by root")
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(DataDirNotWritableError) as excinfo:
            _assert_data_dir_writable(tmp_path)
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

    with patch.object(bootstrap_db_mod.tempfile, "NamedTemporaryFile", _raise):
        with pytest.raises(DataDirNotWritableError) as excinfo:
            _assert_data_dir_writable(tmp_path)
    assert expected_substring in str(excinfo.value)


def test_unknown_errno_falls_back_to_chown_hint():
    """Errnos we don't enumerate still surface the most-likely-cause hint."""
    exc = OSError(errno.EIO, "I/O error")
    advice = _remediation_for(exc)
    assert "chown -R" in advice
    assert "errno=" in advice
