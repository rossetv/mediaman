"""Migration runner for the mediaman database — squashed baseline.

On 2026-05-04 we squashed migrations 1–34 into the baseline ``_SCHEMA``
constant in :mod:`mediaman.db.schema_definition`.  Pre-cutover databases
(``user_version`` between 1 and 33 inclusive) must transit through release
1.8.x — the last release that still contained the per-version migration
files v01–v33 — before upgrading to this version.

Migrations that ship in this release or any later release run as a small
ordered registry walked from :data:`CUTOVER_VERSION` up to
:data:`DB_SCHEMA_VERSION`.  Each entry advances ``user_version`` by one.

Invariants
----------
* ``PRAGMA user_version`` is the authoritative version counter.
* Fresh databases (user_version == 0) receive the full ``_SCHEMA`` DDL
  and are stamped directly at ``DB_SCHEMA_VERSION`` — the baseline already
  reflects every post-cutover migration's resulting schema, so re-running
  the no-op DDL of those migrations would be wasted work.
* Databases at ``CUTOVER_VERSION <= user_version < DB_SCHEMA_VERSION``
  walk the post-cutover registry from their current version upwards.
* Databases at exactly ``DB_SCHEMA_VERSION`` are a no-op.
* Databases at ``0 < user_version < CUTOVER_VERSION`` raise
  :class:`SchemaTooOldError` — the required migration files no longer
  exist and operators must transit through 1.8.x first.
* Databases at ``user_version > DB_SCHEMA_VERSION`` raise
  :class:`SchemaFromFutureError` — the database came from a newer build
  and silently accepting it could corrupt data on the first write.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable

from mediaman.db.schema_definition import _SCHEMA, CUTOVER_VERSION, DB_SCHEMA_VERSION

# Post-cutover migration filenames begin with a zero-padded numeric prefix
# (§9.2) — e.g. ``0035_aes_v1_sunset`` — which is not a valid Python
# identifier, so we cannot use a plain ``from . import 0035_aes_v1_sunset``
# statement.  ``importlib.import_module`` is the standard escape hatch and
# the conventional pattern when migration files follow Django/Alembic
# naming.
_m0035_aes_v1_sunset = importlib.import_module("mediaman.db.migrations.0035_aes_v1_sunset")
_m0036_admin_users_email = importlib.import_module(
    "mediaman.db.migrations.0036_admin_users_email"
)


class SchemaTooOldError(RuntimeError):
    """Raised when the database predates the migration squash cutover.

    The caller must run release 1.8.x to apply the missing migrations
    before upgrading to this version.
    """


class SchemaFromFutureError(RuntimeError):
    """Raised when the database was written by a newer build of mediaman.

    A version number above :data:`DB_SCHEMA_VERSION` means the database
    has been opened by a newer release that may have added columns,
    triggers, or constraints this code does not know about.  Refusing to
    start is safer than silently corrupting data on the first write.
    """


#: Ordered registry of post-cutover migrations.  Each entry is
#: ``(target_version, migration_module)`` where the module exposes
#: ``apply(conn: sqlite3.Connection) -> None``.  Entries MUST be in
#: ascending ``target_version`` order and contiguous with
#: :data:`CUTOVER_VERSION` (i.e. the first entry's target_version is
#: ``CUTOVER_VERSION + 1``).
_POST_CUTOVER: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (35, _m0035_aes_v1_sunset.apply),
    (36, _m0036_admin_users_email.apply),
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance *conn* to :data:`DB_SCHEMA_VERSION`.

    See module docstring for the full behaviour matrix.
    """
    current_version: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current_version == 0:
        # Brand-new database — apply the baseline schema and stamp at the
        # current target.  ``_SCHEMA`` already reflects every post-cutover
        # migration's resulting shape, so running the registry on top
        # would be redundant.
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
        conn.commit()
        return

    if 0 < current_version < CUTOVER_VERSION:
        raise SchemaTooOldError(
            f"Database is at version {current_version} which predates the "
            f"migration cutover ({CUTOVER_VERSION}). Upgrade through "
            f"release 1.8.x first, then this version."
        )

    if current_version > DB_SCHEMA_VERSION:
        raise SchemaFromFutureError(
            f"Database is at version {current_version}, newer than this "
            f"build supports ({DB_SCHEMA_VERSION}). The database was "
            f"likely opened by a newer release of mediaman; downgrading "
            f"is not supported. Restore a backup compatible with this "
            f"version or upgrade mediaman."
        )

    # current_version is in [CUTOVER_VERSION, DB_SCHEMA_VERSION].
    # Walk the post-cutover registry, applying any entry whose target
    # version is strictly greater than the current version, in order.
    for target_version, apply_fn in _POST_CUTOVER:
        if current_version < target_version:
            apply_fn(conn)
            conn.execute(f"PRAGMA user_version={target_version}")
            conn.commit()
            current_version = target_version
