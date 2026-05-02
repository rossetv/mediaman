"""Authentication and keep-action request models.

``LoginRequest`` is the credentials payload accepted by the login
route; ``KeepRequest`` is the snooze-or-keep submission body used by
the digest links and the kept page.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._common import _MAX_PASSWORD_LEN, _MAX_USERNAME_LEN, VALID_KEEP_DURATIONS


class LoginRequest(BaseModel):
    """Login form/JSON body.

    ``extra="forbid"`` makes a stray field (e.g. an attacker shoving
    ``is_admin=true`` into the body) raise HTTP 422 instead of being
    silently ignored.  Length caps prevent a wedged client from
    flooding the auth path with multi-megabyte usernames or passwords;
    the bcrypt path already truncates passwords at 72 bytes, but a
    1 MiB POST still costs CPU and log space before that point.
    """

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=_MAX_USERNAME_LEN)]
    password: Annotated[str, Field(min_length=1, max_length=_MAX_PASSWORD_LEN)]


class KeepRequest(BaseModel):
    """Snooze-or-keep submission body.

    ``duration`` is one of a fixed vocabulary; the
    :data:`VALID_KEEP_DURATIONS` set bounds the value space and the
    field-level cap keeps the payload bounded even when the value is
    not in the allowlist (e.g. an attacker probing with garbage).
    """

    model_config = ConfigDict(extra="forbid")

    duration: Annotated[str, Field(min_length=1, max_length=32)]

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, v: str) -> str:
        if v not in VALID_KEEP_DURATIONS:
            raise ValueError(f"Duration must be one of: {set(VALID_KEEP_DURATIONS)}")
        return v
