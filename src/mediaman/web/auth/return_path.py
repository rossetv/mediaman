"""Which addresses a person may be sent back to after signing in."""

from __future__ import annotations

import re
from urllib.parse import unquote

_MAX_LENGTH = 2048

# Printable ASCII but ``#`` and the backslash, which browsers read as a slash.
_LOCAL_RE = re.compile(r"/[!\"$-\[\]-~]*")

# Not pages: calls, files, sign-in itself, and the list's fragment.
_NOT_PAGES = re.compile(r"/(?:api|static|login|watch/list|healthz|readyz)(?:/|$)")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_return_path(target: str) -> str | None:
    """*target* when it is the address of a page of this app, else None.

    It comes from the address bar, so anything a browser could read as another site, and
    any path that is not a page, is refused. The path is judged as the server would decode it.
    """
    if len(target) > _MAX_LENGTH or not _LOCAL_RE.fullmatch(target):
        return None
    path = unquote(target.partition("?")[0])
    if "\\" in path or "//" in path or _CONTROL_RE.search(path):
        return None
    if any(segment in (".", "..") for segment in path.split("/")):
        return None
    return None if _NOT_PAGES.match(path) else target
