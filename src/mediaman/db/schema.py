"""Back-compat shim: schema definition + migration runner relocated.

The schema DDL constant and version number now live in
:mod:`mediaman.db.schema_definition`; the migration runner lives in
:mod:`mediaman.db.migrations`.  All previously exported names remain
importable from this module so that existing call sites require no changes.
"""

from mediaman.db.migrations import apply_migrations
from mediaman.db.schema_definition import (
    _SCHEMA,
    DB_SCHEMA_VERSION,
)

__all__ = ["DB_SCHEMA_VERSION", "_SCHEMA", "apply_migrations"]
