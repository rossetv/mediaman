"""Bcrypt password hashing, verification, and rotation — re-export barrel.

Split from ``auth/session.py`` (R2). Owns the "how are passwords hashed
and compared" concern; session persistence lives in
:mod:`mediaman.web.auth.session_store`.

This package was promoted from a single ``password_hash.py`` module
when it crossed the 500-line ceiling. It owned three concepts — the
"and" smell — now one private module each:

* :mod:`._user_crud` — admin-user CRUD (``create_user``, ``list_users``,
  ``delete_user``, ``get_user_email``, ``set_user_email``,
  ``user_must_change_password``, ``set_must_change_password``) plus the
  :class:`UserExistsError` / :class:`UserRecord` vocabulary.
* :mod:`._authenticate` — the credential-verification path
  (``authenticate`` and its short-circuit / bcrypt-verify helpers).
* :mod:`._change_password` — password rotation (``change_password`` and
  its pre-/post-transaction guard helpers).

The bcrypt-input preparation pipeline, the cached dummy hash, the
rollback sentinels, and the log-field sanitiser live in
:mod:`mediaman.web.auth._password_hash_helpers` — a sibling, not a
package member, because ``reauth.py`` and ``routes/auth.py`` consume it
too. This barrel re-exports the public surface unchanged so every
``from mediaman.web.auth.password_hash import X`` keeps working.

Bcrypt 72-byte truncation defence
---------------------------------

``bcrypt.hashpw`` silently truncates its input to 72 bytes — two
different 100-byte passwords whose first 72 bytes match would hash to
the same value, and an attacker who knows this can craft pathological
inputs. We defeat that by pre-hashing any password that exceeds 72
bytes (after Unicode NFKC normalisation) with SHA-256, base64-encoding
the digest (44 bytes — comfortably under bcrypt's limit) and feeding
the result into bcrypt. Two distinct long passwords therefore have
distinct SHA-256 digests and bcrypt sees full-entropy inputs.

The gate is keyed on input length so existing ``admin_users`` rows
hashed before this change landed continue to verify: any password short
enough for bcrypt to accept directly (≤ 72 bytes) bypasses the pre-hash
on both the set and verify paths and hits the same bytes the original
``hashpw`` did. No backfill or lazy migration is required — the only
behaviour change is for pathological inputs over 72 bytes, which
nobody could ever have logged in with reliably anyway.

Both ``hashpw`` and ``checkpw`` callers MUST go through
``_prepare_bcrypt_input`` so the pre-hash logic is applied
symmetrically. A mismatch (pre-hash on set, raw on verify or vice
versa) would lock everyone out.
"""

from __future__ import annotations

from mediaman.web.auth._password_hash_helpers import BCRYPT_ROUNDS
from mediaman.web.auth.password_hash._authenticate import authenticate
from mediaman.web.auth.password_hash._change_password import change_password
from mediaman.web.auth.password_hash._user_crud import (
    UserExistsError,
    UserRecord,
    create_user,
    delete_user,
    get_user_email,
    list_users,
    set_must_change_password,
    set_user_email,
    user_must_change_password,
)

__all__ = [
    "BCRYPT_ROUNDS",
    "UserExistsError",
    "UserRecord",
    "authenticate",
    "change_password",
    "create_user",
    "delete_user",
    "get_user_email",
    "list_users",
    "set_must_change_password",
    "set_user_email",
    "user_must_change_password",
]
