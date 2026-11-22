"""The credential-verification path.

Owns `authenticate` (the login check, gated by the login lockout) and
`verify_password` (the bare password check behind it, which re-auth and
password change use with their own counter). The "constant time across
the meaningful branches" property — a real-username probe and a
fake-username probe burn the same bcrypt latency — is implemented here:
see `verify_password`'s dummy bcrypt cycle.

User CRUD lives in `._user_crud`; password rotation in
`._change_password`.
"""

from __future__ import annotations

import logging
import sqlite3

import bcrypt

from mediaman.web.auth._password_hash_helpers import (
    _get_dummy_hash,
    _prepare_bcrypt_input,
)

logger = logging.getLogger(__name__)


def _short_circuit_authenticate(
    conn: sqlite3.Connection,
    username: str,
    client_ip: str,
) -> bool | None:
    """Return a definite answer for the no-bcrypt fast paths.

    Returns ``False`` when the request is rejected without bcrypt
    (empty username, or the username is locked from *client_ip*). Returns ``None`` when the
    caller must continue with the bcrypt verification path.

    The locked branch bumps `record_failure` so the escalation thresholds
    (5 → 10 → 15 failures → 15 min / 1 h / 24 h) stay reachable: counting
    the continued hammering is what keeps the anti-DoS band-promotion
    design working. See ``test_login_lockout.py``.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, login_ip_key, record_failure

    # Reject empty usernames before touching bcrypt. Otherwise an
    # unauthenticated attacker can stream empty-username requests at
    # the login endpoint and burn server CPU at one bcrypt round per
    # request — a cheap CPU-DoS.
    if not username:
        return False

    # Check lockout first. A locked account already has a "no" answer
    # without re-running bcrypt — skip the dummy round and save the
    # CPU. record_failure keeps acquiring the writer lock, which is
    # the price of the escalation property.
    key = login_ip_key(username, client_ip)
    if is_locked_out(conn, key):
        record_failure(conn, key)
        logger.warning(
            "auth.account_locked user=%s ip=%s reason=lockout_active", username, client_ip
        )
        return False

    return None


def verify_password(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Check *password* against the stored hash for *username*.

    Reads and writes no failure counter and ignores any lock: callers
    bring their own. Burns a constant-time dummy bcrypt cycle on the
    "user not found" path so a real-username probe and a fake-username
    probe take ~the same wall time — timing cannot enumerate valid
    usernames.
    """
    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()
    if row is None:
        bcrypt.checkpw(_prepare_bcrypt_input(password), _get_dummy_hash())
        return False
    return bcrypt.checkpw(_prepare_bcrypt_input(password), row["password_hash"].encode())


def _verify_credentials_against_db(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    client_ip: str,
) -> bool:
    """Run `verify_password` and update the login failure counter for *client_ip*."""
    from mediaman.web.auth.login_lockout import login_ip_key, record_failure, record_success

    key = login_ip_key(username, client_ip)
    ok = verify_password(conn, username, password)
    if ok:
        record_success(conn, key)
    else:
        record_failure(conn, key)
    return ok


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    client_ip: str,
) -> bool:
    """Verify username/password credentials for a login from *client_ip*.

    The lockout counts failures per username and client IP (see
    `mediaman.web.auth.login_lockout`), so wrong guesses from one address
    do not lock the user out from another.

    Always performs a bcrypt check — even for nonexistent users — to
    prevent timing-based username enumeration.

    Two short-circuit paths skip the bcrypt cycle deliberately:

    * Empty username — there is no user to authenticate, and burning a
      bcrypt round per request would let an unauthenticated attacker
      DoS the server's CPU by streaming empty-username login attempts.
    * Username already locked from this IP — `mediaman.web.auth.login_lockout`
      already knows the answer is "no" without re-checking the hash.
      Skipping bcrypt here cuts the cost of a sustained brute-force
      hammering a locked account from one bcrypt round per attempt
      to zero. The ``record_failure`` writer-lock acquisition is
      retained even on the locked path because the escalation
      thresholds (5 → 10 → 15 failures → 15 min / 1 h / 24 h) are
      reachable only while the counter keeps climbing during the
      lock window — see ``test_login_lockout.py``.
      Without the continued counter bump the escalation-window logic in
      `mediaman.web.auth.login_lockout` cannot escalate to the 1-hour
      and 24-hour windows.

    The "constant-time" property is preserved across the *meaningful*
    branches: an attacker sending a real username gets the same
    bcrypt-cycle latency whether the user exists or not, since the
    "user not found" path still burns a dummy bcrypt round. The empty
    and locked paths are a different (and visible) latency, but the
    information they leak — "you sent no username" or "this account is
    rate-limited" — is information the attacker already controls or
    can deduce from response volume anyway.
    """
    short_circuit = _short_circuit_authenticate(conn, username, client_ip)
    if short_circuit is not None:
        return short_circuit
    return _verify_credentials_against_db(conn, username, password, client_ip)
