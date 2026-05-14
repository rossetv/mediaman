"""The credential-verification path.

Owns :func:`authenticate` and its two private helpers. The "constant
time across the meaningful branches" property — a real-username probe
and a fake-username probe burn the same bcrypt latency — is implemented
here: see :func:`_verify_credentials_against_db`'s dummy bcrypt cycle.

User CRUD lives in :mod:`._user_crud`; password rotation in
:mod:`._change_password`.
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
    *,
    record_failures: bool,
) -> bool | None:
    """Return a definite answer for the no-bcrypt fast paths.

    Returns ``False`` when the request is rejected without bcrypt
    (empty username, or locked account). Returns ``None`` when the
    caller must continue with the bcrypt verification path.

    The locked branch still bumps :func:`record_failure` so the
    escalation thresholds (5 → 10 → 15 failures → 15 min / 1 h / 24 h)
    remain reachable while the lock window is active — see C6 in
    ``test_login_lockout.py``.
    """
    from mediaman.web.auth.login_lockout import is_locked_out, record_failure

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
    if is_locked_out(conn, username):
        if record_failures:
            record_failure(conn, username)
        logger.warning("auth.account_locked user=%s reason=lockout_active", username)
        return False

    return None


def _verify_credentials_against_db(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    record_failures: bool,
) -> bool:
    """Do the bcrypt-verify dance against the stored hash for *username*.

    Burns a constant-time dummy bcrypt cycle on the "user not found"
    path so a real-username probe and a fake-username probe take ~the
    same wall time — timing cannot enumerate valid usernames.
    """
    from mediaman.web.auth.login_lockout import record_failure, record_success

    row = conn.execute(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    ).fetchone()

    if row is None:
        bcrypt.checkpw(_prepare_bcrypt_input(password), _get_dummy_hash())
        if record_failures:
            record_failure(conn, username)
        return False

    ok = bcrypt.checkpw(_prepare_bcrypt_input(password), row["password_hash"].encode())
    if ok:
        record_success(conn, username)
    elif record_failures:
        record_failure(conn, username)
    return ok


def authenticate(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    *,
    record_failures: bool = True,
) -> bool:
    """Verify username/password credentials.

    Always performs a bcrypt check — even for nonexistent users — to
    prevent timing-based username enumeration.

    Two short-circuit paths skip the bcrypt cycle deliberately:

    * Empty username — there is no user to authenticate, and burning a
      bcrypt round per request would let an unauthenticated attacker
      DoS the server's CPU by streaming empty-username login attempts.
    * Account already locked — :mod:`mediaman.web.auth.login_lockout`
      already knows the answer is "no" without re-checking the hash.
      Skipping bcrypt here cuts the cost of a sustained brute-force
      hammering a locked account from one bcrypt round per attempt
      to zero. The ``record_failure`` writer-lock acquisition is
      retained even on the locked path because the escalation
      thresholds (5 → 10 → 15 failures → 15 min / 1 h / 24 h) are
      reachable only while the counter keeps climbing during the
      lock window — see the C6 test in ``test_login_lockout.py``.
      Without the continued counter bump the escalation-window logic in
      :mod:`mediaman.web.auth.login_lockout` cannot escalate to the 1-hour
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
    short_circuit = _short_circuit_authenticate(conn, username, record_failures=record_failures)
    if short_circuit is not None:
        return short_circuit
    return _verify_credentials_against_db(conn, username, password, record_failures=record_failures)
