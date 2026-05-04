"""Pydantic request-body models for user-management routes.

These were previously defined inline in ``mediaman.web.routes.users`` as
private classes.  Moving them here keeps route modules thin and makes the
models discoverable from the ``mediaman.web.models`` package.
"""

from __future__ import annotations

from pydantic import BaseModel


class CreateUserBody(BaseModel):
    """Body shape for ``POST /api/users``."""

    username: str = ""
    password: str = ""


class ChangePasswordBody(BaseModel):
    """Body shape for ``POST /api/users/change-password``."""

    old_password: str = ""
    new_password: str = ""


class ReauthBody(BaseModel):
    """Body shape for ``POST /api/auth/reauth``."""

    password: str = ""
