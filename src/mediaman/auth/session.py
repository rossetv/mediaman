"""Admin user and session management."""

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt

from mediaman.crypto import generate_session_token

_DUMMY_HASH: bytes | None = None
_DUMMY_HASH_LOCK = threading.Lock()

_EXPIRED_CLEANUP_INTERVAL = 60.0  # seconds between expired-session sweeps
_last_cleanup_at = 0.0
_cleanup_lock = threading.Lock()


def _get_dummy_hash() -> bytes:
    """Lazily compute the bcrypt dummy hash the first time it's needed.

    Running ``bcrypt.gensalt`` at import time imposes ~300ms on every
    process start (including short-lived CLI invocations). Computing it
    on demand keeps ``mediaman create-user`` snappy.
    """
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12))
        return _DUMMY_HASH


def create_user(conn: sqlite3.Connection, username: str, password: str) -> None:
    """Insert an admin user with a bcrypt-hashed password (cost 12).

    Raises ValueError if the username already exists.
    """
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"User '{username}' already exists")


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Verify username/password credentials.

    Always performs a bcrypt check — even for nonexistent users — to prevent
    timing-based username enumeration.
    """
    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        # Constant-time dummy check so the response time doesn't leak user existence.
        bcrypt.checkpw(password.encode(), _get_dummy_hash())
        return False

    return bcrypt.checkpw(password.encode(), row["password_hash"].encode())


def create_session(
    conn: sqlite3.Connection, username: str, ttl_seconds: int = 86400
) -> str:
    """Create a session for the given username and return the token.

    The token is a 64-character hex string. The session expires after
    ``ttl_seconds`` seconds from now.
    """
    token = generate_session_token()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute(
        "INSERT INTO admin_sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, username, now.isoformat(), expires_at),
    )
    conn.commit()
    return token


def validate_session(conn: sqlite3.Connection, token: str) -> str | None:
    """Return the username for a valid, non-expired session token.

    Sweeps expired session rows at most once per ``_EXPIRED_CLEANUP_INTERVAL``
    seconds so hot traffic doesn't turn every request into a write.
    Returns None if the token is missing or expired.
    """
    global _last_cleanup_at
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    mono = time.monotonic()
    if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
        with _cleanup_lock:
            if mono - _last_cleanup_at >= _EXPIRED_CLEANUP_INTERVAL:
                conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now_iso,))
                conn.commit()
                _last_cleanup_at = mono

    row = conn.execute(
        "SELECT username, expires_at FROM admin_sessions WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] <= now_iso:
        return None
    return row["username"]


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    """Delete the session row for the given token (logout)."""
    conn.execute("DELETE FROM admin_sessions WHERE token=?", (token,))
    conn.commit()


def change_password(conn: sqlite3.Connection, username: str, old_password: str, new_password: str) -> bool:
    """Change a user's password. Returns True on success, False if old password is wrong."""
    if not authenticate(conn, username, old_password):
        return False
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    conn.execute(
        "UPDATE admin_users SET password_hash=? WHERE username=?",
        (new_hash, username),
    )
    # Invalidate all existing sessions for this user
    conn.execute("DELETE FROM admin_sessions WHERE username=?", (username,))
    conn.commit()
    return True


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Return all admin users (without password hashes)."""
    rows = conn.execute(
        "SELECT id, username, created_at FROM admin_users ORDER BY id"
    ).fetchall()
    return [{"id": row["id"], "username": row["username"], "created_at": row["created_at"]} for row in rows]


def delete_user(conn: sqlite3.Connection, user_id: int, current_username: str) -> bool:
    """Delete an admin user by ID.

    Refuses to delete the current user or the last remaining admin —
    either would lock everyone out of the UI.

    The "last admin" check is enforced atomically inside the DELETE
    statement itself (``WHERE ... AND (SELECT COUNT(*) FROM admin_users) > 1``)
    so two concurrent requests cannot both pass a TOCTOU check and wipe
    the final pair of admins.
    """
    row = conn.execute("SELECT username FROM admin_users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return False
    if row["username"] == current_username:
        return False  # Can't delete yourself

    # Wrap both writes in an explicit transaction so, if the atomic admin
    # DELETE is refused (last-admin guard), the pre-emptive session wipe
    # is rolled back alongside it.
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Sessions must go first — admin_sessions.username has a FK to
        # admin_users(username) and FK checks are ON.
        conn.execute(
            "DELETE FROM admin_sessions WHERE username=?", (row["username"],)
        )
        # Atomic last-admin guard: delete only if at least one other
        # admin still exists. Concurrent callers can't both see "count > 1"
        # because the subquery is evaluated as part of the DELETE.
        cursor = conn.execute(
            "DELETE FROM admin_users WHERE id = ? "
            "AND (SELECT COUNT(*) FROM admin_users) > 1",
            (user_id,),
        )
        if cursor.rowcount == 0:
            conn.execute("ROLLBACK")
            return False  # Would have left zero admins — refuse.
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True
