"""Back-compat shim — DB bootstrap logic now lives in :mod:`mediaman.bootstrap.data_dir`.

This module re-exports the public API so existing imports such as::

    from mediaman.bootstrap.db import bootstrap_db, DataDirNotWritableError
    from mediaman.bootstrap import db as bootstrap_db_mod

continue to work without change.

``tempfile`` is imported explicitly here because tests patch it via
``patch.object(bootstrap_db_mod.tempfile, ...)``.
"""

import tempfile  # kept for test monkeypatching: patch.object(bootstrap_db_mod.tempfile, ...)

from mediaman.app_factory import bootstrap_db
from mediaman.bootstrap.data_dir import (
    DataDirNotWritableError,
    _assert_data_dir_writable,
    _remediation_for,
)

__all__ = [
    "DataDirNotWritableError",
    "_assert_data_dir_writable",
    "_remediation_for",
    "bootstrap_db",
    "tempfile",
]
