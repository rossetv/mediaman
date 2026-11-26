"""Errors raised from a frame that belongs to plexapi, as its parser raises them."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_namespace: dict[str, Any] = {"__name__": "plexapi.library"}
exec(compile("def raise_it(exc):\n    raise exc\n", "<plexapi>", "exec"), _namespace)


def raised_by_plexapi(exc: Exception) -> Callable[..., None]:
    """A callable that raises ``exc`` from a function whose module is plexapi's."""
    return lambda *_args, **_kwargs: _namespace["raise_it"](exc)
