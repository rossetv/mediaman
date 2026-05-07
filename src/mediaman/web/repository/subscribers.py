"""Repository functions for subscriber CRUD operations.

All reads and writes against the ``subscribers`` table live here.
The route layer retains only auth-style transaction orchestration
(BEGIN IMMEDIATE / COMMIT / ROLLBACK for the concurrent-insert race).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SubscriberRow:
    """A single row from the subscribers table."""

    id: int
    email: str
    active: bool
    created_at: str


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_subscribers(conn: sqlite3.Connection) -> list[SubscriberRow]:
    """Return all subscribers ordered by creation date."""
    rows = conn.execute(
        "SELECT id, email, active, created_at FROM subscribers ORDER BY created_at ASC"
    ).fetchall()
    return [
        SubscriberRow(
            id=r["id"],
            email=r["email"],
            active=bool(r["active"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def find_subscriber_by_email(conn: sqlite3.Connection, email: str) -> int | None:
    """Return the subscriber id for the given email, or None if not found."""
    row = conn.execute("SELECT id FROM subscribers WHERE email = ?", (email,)).fetchone()
    return row["id"] if row is not None else None


def find_subscriber_by_id(conn: sqlite3.Connection, subscriber_id: int) -> str | None:
    """Return the email for the given subscriber id, or None if not found."""
    row = conn.execute("SELECT email FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()
    return row["email"] if row is not None else None


def find_subscriber_status_by_email(
    conn: sqlite3.Connection, email: str
) -> tuple[int, bool] | None:
    """Return (id, active) for the given email, or None if not found."""
    row = conn.execute("SELECT id, active FROM subscribers WHERE email = ?", (email,)).fetchone()
    if row is None:
        return None
    return row["id"], bool(row["active"])


def fetch_active_subscribers_in(conn: sqlite3.Connection, emails: set[str]) -> list[str]:
    """Return the subset of ``emails`` that are active subscribers.

    Uses a batched IN-clause against the UNIQUE INDEX on subscribers.email.
    The column is normalised to lowercase on write and carries COLLATE NOCASE,
    so no lower() wrapper is needed — that would defeat the index.
    """
    if not emails:
        return []
    placeholders = ",".join("?" * len(emails))
    rows = conn.execute(
        f"SELECT email FROM subscribers WHERE active=1 AND email IN ({placeholders})",
        tuple(emails),
    ).fetchall()
    return [r["email"] for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def add_subscriber(conn: sqlite3.Connection, *, email: str, now: str) -> None:
    """Insert a new active subscriber row.

    The caller is responsible for wrapping this in a transaction (BEGIN
    IMMEDIATE) to prevent a race condition with a concurrent add.
    """
    conn.execute(
        "INSERT INTO subscribers (email, active, created_at) VALUES (?, 1, ?)",
        (email, now),
    )


def delete_subscriber(conn: sqlite3.Connection, subscriber_id: int) -> None:
    """Delete a subscriber row by primary key."""
    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))


def deactivate_subscriber(conn: sqlite3.Connection, subscriber_id: int) -> None:
    """Set active=0 for a subscriber (used by the public unsubscribe flow)."""
    conn.execute("UPDATE subscribers SET active = 0 WHERE id = ?", (subscriber_id,))
