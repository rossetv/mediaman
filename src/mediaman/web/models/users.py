"""Pydantic request-body models for user-management routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreateUserBody(BaseModel):
    """Body shape for ``POST /api/users``."""

    username: str = ""
    password: str = ""


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

    old_password: str = ""
    new_password: str = ""


class ReauthBody(BaseModel):
    """Body shape for ``POST /api/auth/reauth``."""

    password: str = ""
