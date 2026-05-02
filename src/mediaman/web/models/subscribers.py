"""Subscriber-management request models."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._common import _EMAIL_RE, _MAX_EMAIL_LEN, _reject_crlf


class SubscriberCreate(BaseModel):
    """Subscriber-creation payload (admin only).

    The matching admin-side route currently accepts ``email`` via a
    form field and runs its own regex validator (``_validate_email``);
    this model is the canonical schema for any future JSON consumer,
    and mirrors the route's checks at the type layer.

    ``extra="forbid"`` blocks an attacker shoving extra fields into
    the body (e.g. ``unsubscribed=False`` to bypass an opt-out).  The
    ``max_length=320`` cap matches the RFC 5321 maximum for an email
    address and prevents a multi-megabyte string slipping through to
    the SQLite write.
    """

    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=_MAX_EMAIL_LEN)]

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Normalise + check the address against the same regex used by
        the route helper.  Header-injection characters (CR/LF/NUL) are
        already excluded by the regex (``[A-Za-z0-9._%+-]`` does not
        admit them) but ``_reject_crlf`` is applied first so the error
        message matches the shape used elsewhere."""
        v = v.strip().lower()
        _reject_crlf(v)
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v
