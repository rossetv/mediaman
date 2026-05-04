"""Shared SQLite helpers used across multiple migration functions.

These utilities abstract the three patterns that appear repeatedly in the
migration chain:

* :func:`_table_exists` — check whether a table has been created yet.
* :func:`_column_exists` — check whether a column is present in a table.
* :func:`_recreate_table` — the standard SQLite "rename-copy-drop" table
  rebuild pattern required whenever a column constraint must be changed (SQLite
  does not support ``ALTER TABLE … ALTER COLUMN``).

All functions take a plain :class:`sqlite3.Connection` and are deliberately
side-effect-free except where documented.  They do NOT manage transactions;
callers are responsible for wrapping calls in ``BEGIN`` / ``COMMIT`` /
``ROLLBACK`` blocks as appropriate.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Return ``True`` if a table named *name* exists in the database.

    Uses ``sqlite_master`` rather than ``PRAGMA table_info`` so that the
    check works correctly even when foreign-key enforcement is disabled —
    the pragma only returns rows for tables that pass the FK integrity
    check at open time on some SQLite builds.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """Return ``True`` if *col* is a column in *table*.

    Queries ``PRAGMA table_info(table)`` and checks the ``name`` field
    (index 1 in the returned tuple) against *col*.  The comparison is
    case-sensitive, matching SQLite's own column resolution behaviour.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == col for row in rows)


def _recreate_table(
    conn: sqlite3.Connection,
    table: str,
    new_ddl: str,
    column_map: dict[str, str],
    *,
    extra_sql: list[str] | None = None,
) -> None:
    """Rebuild *table* using the standard SQLite rename-copy-drop pattern.

    This is the only way to change column constraints (e.g. add or remove
    ``NOT NULL``, change a ``REFERENCES`` clause) in SQLite, which does not
    support ``ALTER TABLE … ALTER COLUMN``.

    The procedure:

    1. Disable foreign-key enforcement (required by SQLite while the old
       table still exists with its FK constraints).
    2. Create a temporary table ``<table>_new`` using *new_ddl*.
    3. Copy every row from the old table, mapping old column names to new
       ones via *column_map* (``{"new_name": "old_name"}``).  Columns that
       share the same name in both tables need no entry in *column_map* — they
       are copied verbatim.
    4. Drop the old table.
    5. Rename ``<table>_new`` → *table*.
    6. Execute any *extra_sql* statements (typically ``CREATE INDEX`` calls).
    7. Re-enable foreign-key enforcement unconditionally in a ``finally``
       block so the database is never left in a permissive state.

    :param conn:       Open database connection.
    :param table:      Name of the table to rebuild.
    :param new_ddl:    ``CREATE TABLE <table>_new (…)`` statement.
    :param column_map: Mapping of ``{new_col: old_col}`` for renamed columns.
                       Columns with the same name in both tables are handled
                       automatically and need not appear here.
    :param extra_sql:  Optional list of SQL statements to run after renaming
                       (e.g. index creation).
    """
    tmp = f"{table}_new"
    # Derive the column list from the existing table. We cannot PRAGMA the new
    # table before creating it, so the INSERT column list is built from
    # old_cols: walk each old column, apply the column_map if there is a rename,
    # and collect the result.  Columns with the same name in both tables need no
    # entry in column_map.
    old_cols: list[str] = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]

    # Build the SELECT list aligned with the INSERT target column list.
    # Strategy: walk old columns; if a new name maps to this old name, emit
    # the new name as target and old name as source; otherwise emit the old
    # name for both (assuming it exists in the new schema).
    reverse_map: dict[str, str] = {v: k for k, v in column_map.items()}

    insert_cols: list[str] = []
    select_exprs: list[str] = []
    for old_col in old_cols:
        new_col = reverse_map.get(old_col, old_col)
        insert_cols.append(new_col)
        select_exprs.append(old_col)

    insert_clause = ", ".join(insert_cols)
    select_clause = ", ".join(select_exprs)

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(new_ddl)
        conn.execute(f"INSERT INTO {tmp} ({insert_clause}) SELECT {select_clause} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        for sql in extra_sql or []:
            conn.execute(sql)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
