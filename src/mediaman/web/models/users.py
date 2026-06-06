"""Pydantic request-body models for user-management routes."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ._common import _MAX_PASSWORD_LEN, _MAX_USERNAME_LEN


class CreateUserBody(BaseModel):
    """Body shape for ``POST /api/users``."""

    username: Annotated[str, Field(min_length=1, max_length=_MAX_USERNAME_LEN)]
    password: Annotated[str, Field(min_length=1, max_length=_MAX_PASSWORD_LEN)]


class UpdateEmailBody(BaseModel):
    """Body shape for ``PATCH /api/users/me/email``.

    Empty string means "clear my email"; a non-empty value triggers
    validation in the repository layer (``set_user_email`` raises
    ``ValueError`` on a malformed address).

    The 320-character cap matches RFC 5321 and the same cap enforced by
    :func:`mediaman.core.email_validation.validate_email_address` — a
    larger value would be rejected anyway, so the Pydantic field-level
    cap fails it earlier with a clearer error.
    """

    model_config = ConfigDict(extra="forbid")
    email: str = Field(default="", max_length=320)


class ChangePasswordBody(BaseModel):
    """Body shape for ``POST /api/users/change-password``."""

    old_password: Annotated[str, Field(min_length=1, max_length=_MAX_PASSWORD_LEN)]
    new_password: Annotated[str, Field(min_length=1, max_length=_MAX_PASSWORD_LEN)]


class ReauthBody(BaseModel):
    """Body shape for ``POST /api/auth/reauth``."""

    password: Annotated[str, Field(min_length=1, max_length=_MAX_PASSWORD_LEN)]
