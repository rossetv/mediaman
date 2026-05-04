"""Single source of truth for the JSON response envelope shape.

Every web route that returns a structured JSON response MUST use these
helpers rather than constructing ``JSONResponse`` objects directly with
ad-hoc keys.  The canonical envelope is::

    # Success
    {"ok": true, ...extra_fields}

    # Error
    {"ok": false, "error": "<machine-readable code>", "message": "<optional human prose>", ...extra}

Helpers
-------
``respond_ok``
    Wrap a success payload in the canonical envelope.

``respond_err``
    Wrap an error in the canonical envelope.  The ``error`` field is
    intended for a short, machine-readable code (e.g.
    ``"invalid_or_expired"``, ``"reauth_required"``).  Human-readable
    prose belongs in the optional ``message`` field.  Pass arbitrary
    extra keyword arguments to include additional fields (e.g.
    ``reauth_required=True``, ``issues=[...]``).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


def respond_ok(data: dict | None = None, *, status: int = 200) -> JSONResponse:
    """Return a canonical success envelope.

    The body is ``{"ok": true, **data}`` when *data* is provided, or
    just ``{"ok": true}`` when *data* is ``None``.

    Args:
        data:   Optional extra fields to merge into the envelope.
        status: HTTP status code (default 200).

    Returns:
        A :class:`~fastapi.responses.JSONResponse` with the envelope body.
    """
    body: dict = {"ok": True}
    if data:
        body.update(data)
    return JSONResponse(body, status_code=status)


def respond_err(
    error: str,
    *,
    status: int = 400,
    message: str | None = None,
    **extra: object,
) -> JSONResponse:
    """Return a canonical error envelope.

    The body is ``{"ok": false, "error": error}`` with optional
    ``"message"`` and any *extra* keyword arguments merged in.

    Args:
        error:   Short, machine-readable error code.
        status:  HTTP status code (default 400).
        message: Optional human-readable prose to include under
                 ``"message"``.  Omitted from the body when ``None``.
        **extra: Additional fields to include in the body verbatim
                 (e.g. ``reauth_required=True``, ``issues=[...]``).

    Returns:
        A :class:`~fastapi.responses.JSONResponse` with the envelope body.
    """
    body: dict = {"ok": False, "error": error}
    if message is not None:
        body["message"] = message
    body.update(extra)
    return JSONResponse(body, status_code=status)
