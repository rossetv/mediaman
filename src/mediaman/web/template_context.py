"""What every page's template is given about the signed-in person."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import Request

from mediaman.db import get_db
from mediaman.web.repository.avatars import avatar_url

logger = logging.getLogger(__name__)


def signed_in_avatar(request: Request) -> dict[str, str | None]:
    username = getattr(request.state, "username", None)
    if username is None:
        return {"avatar_url": None}
    try:
        return {"avatar_url": avatar_url(get_db(), username)}
    except (sqlite3.Error, RuntimeError):
        # rationale: a page must still be drawn, with initials, when the picture cannot be read.
        logger.warning("could not read a person's avatar for the menu", exc_info=True)
        return {"avatar_url": None}
