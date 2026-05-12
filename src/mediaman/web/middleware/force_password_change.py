"""Funnel session-bearing requests from flagged admins to the password-
change page.

When an admin signs in with a plaintext password that fails the current
strength policy, the login route flips ``must_change_password`` on
their row. This middleware intercepts every subsequent cookie-bearing
request and routes it to ``/force-password-change`` until the flag is
cleared. See :class:`ForcePasswordChangeMiddleware` for the full set of
allowed paths.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class ForcePasswordChangeMiddleware(BaseHTTPMiddleware):
    """Funnel flagged admins to /force-password-change.

    When an admin signs in with a plaintext password that fails the
    current strength policy, ``auth_routes.login_submit`` flips the
    ``must_change_password`` flag on their row. Any subsequent
    request that carries their session cookie gets intercepted here:

    - ``GET`` requests for anything other than the force-change page,
      static assets, logout, or the login page itself are 302-
      redirected to ``/force-password-change``.
    - ``POST`` / state-changing methods get a 403 JSON response so
      JS callers see a clean failure rather than a redirect.

    The check is cheap: cookie lookup + single-row SELECT; no
    validation, no HMAC, no crypto.
    """

    # Paths that are always allowed even when a user is flagged —
    # the force-change page itself, its POST, static assets, logout,
    # the login page (so the user can switch accounts if they don't
    # remember their own password), and the kubelet/Docker probes
    # (which carry no session cookie of their own but might collide
    # with one from a stale browser tab on the same origin and would
    # otherwise be redirected away from a 200 healthcheck reply).
    _ALLOWED_PREFIXES = (
        "/force-password-change",
        "/static/",
        "/login",
        "/api/auth/logout",
        "/healthz",
        "/readyz",
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Redirect to the password-change page when the session user must change their password.

        Passes requests through unchanged when there is no session cookie, when
        the path is in the allowed-prefix list, or when the session is invalid.
        For all other authenticated requests it checks the ``must_change_password``
        flag and redirects to ``/force-password-change`` when set.
        """
        token = request.cookies.get("session_token")
        if not token:
            return await call_next(request)

        path = request.url.path
        if any(path == p or path.startswith(p) for p in self._ALLOWED_PREFIXES):
            return await call_next(request)

        # Cheap check — avoid importing the DB layer at module load
        # to keep this middleware testable in isolation.
        try:
            from mediaman.db import get_db
            from mediaman.services.rate_limit import get_client_ip
            from mediaman.web.auth.password_hash import user_must_change_password
            from mediaman.web.auth.session_store import validate_session
        except ImportError:
            # If the DB / auth submodules cannot even be imported the whole
            # app is in a broken state — the middleware cannot meaningfully
            # gate the request, so fall through. Anything more aggressive
            # would mask the real startup failure with a misleading 500.
            return await call_next(request)

        try:
            conn = get_db()
        except RuntimeError:
            return await call_next(request)

        user_agent = request.headers.get("user-agent", "")
        client_ip = get_client_ip(request)
        username = validate_session(
            conn,
            token,
            user_agent=user_agent,
            client_ip=client_ip,
        )
        if username is None:
            return await call_next(request)

        if not user_must_change_password(conn, username):
            return await call_next(request)

        # Flagged: funnel.
        if request.method == "GET":
            return Response(
                status_code=302,
                headers={"Location": "/force-password-change"},
            )
        import json as _json

        body = _json.dumps(
            {
                "detail": "password_change_required",
                "message": "You must change your password before continuing.",
                "redirect": "/force-password-change",
            }
        ).encode()
        return Response(
            content=body,
            status_code=403,
            media_type="application/json",
        )
