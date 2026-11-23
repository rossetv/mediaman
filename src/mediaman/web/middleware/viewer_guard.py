"""Default-deny gate for accounts whose role is not ``admin``.

Every request with a valid session gets ``request.state.username`` and
``request.state.user_role`` here, read fresh from the database so a
demotion applies on the very next request. A non-admin reaches only the
routes on the viewer allowlist; any other page redirects to ``/watch``
and any other call answers 403 before the route runs.

The allowlist is matched on HTTP method plus the matched route's path
template, never on the raw path, so a lookalike such as ``/watchx`` or a
dot-segment such as ``/api/watch/../settings`` cannot borrow an
allowlisted prefix: it matches no allowlisted route and is refused.
"""

from __future__ import annotations

from fastapi.routing import iter_route_contexts
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Match

from mediaman.db import get_db
from mediaman.web.auth.middleware import resolve_cached_session
from mediaman.web.auth.password_hash import get_user_role

#: ``(method, path template)`` pairs a viewer may reach. The public token
#: pages, login, logout, health and force-password-change need no session
#: at all; they are listed so a viewer whose browser sends its cookie
#: along is not turned away from them.
_VIEWER_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/watch"),
        ("GET", "/watch/show/{show_key}"),
        ("GET", "/watch/movie/{key}"),
        ("GET", "/watch/play/{rating_key}"),
        ("GET", "/api/watch/search"),
        ("POST", "/api/watch/keep"),
        ("POST", "/api/watch/unkeep"),
        ("GET", "/api/watch/thumb/{rating_key}"),
        ("POST", "/api/watch/stream"),
        ("GET", "/api/watch/stream/{stream_id}/index.m3u8"),
        ("GET", "/api/watch/stream/{stream_id}/seg/{name}"),
        ("POST", "/api/watch/stream/{stream_id}/stop"),
        ("POST", "/api/watch/progress"),
        ("GET", "/search"),
        ("GET", "/api/search"),
        ("GET", "/api/search/discover"),
        ("GET", "/api/search/detail/{media_type}/{tmdb_id}"),
        ("POST", "/api/search/download"),
        ("GET", "/downloads"),
        ("GET", "/api/downloads"),
        ("GET", "/api/download/status"),
        ("GET", "/recommended"),
        ("GET", "/api/recommended"),
        ("POST", "/api/recommended/{recommendation_id}/download"),
        ("GET", "/api/poster/{rating_key}"),
        ("GET", "/account"),
        ("POST", "/api/users/change-password"),
        ("POST", "/api/auth/reauth"),
        ("GET", "/login"),
        ("POST", "/login"),
        ("POST", "/api/auth/logout"),
        ("GET", "/force-password-change"),
        ("POST", "/force-password-change"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/keep/{token}"),
        ("POST", "/keep/{token}"),
        ("GET", "/download/{token}"),
        ("POST", "/download/{token}"),
        ("GET", "/unsubscribe"),
        ("POST", "/unsubscribe"),
    }
)

#: Template prefixes a viewer may reach with any method. Each ends in
#: ``/`` so it covers whole path segments only.
_VIEWER_PREFIXES: tuple[str, ...] = ("/static/",)


def _matched_template(request: Request) -> str | None:
    """Return the path template of the route that will serve *request*, if any.

    Only a full match (path and method) counts, as the router would pick
    it; a path served under another method has no template here.
    """
    for route in iter_route_contexts(request.app.routes):
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            template: str | None = route.path_format
            return template
    return None


def _viewer_may_reach(method: str, template: str | None) -> bool:
    """True when a non-admin may reach the route *template* with *method*."""
    if template is None:
        return False
    if (method, template) in _VIEWER_ROUTES:
        return True
    return template.startswith(_VIEWER_PREFIXES)


class ViewerGuardMiddleware(BaseHTTPMiddleware):
    """Put the session's user and role on the request; refuse non-admins off the allowlist."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = request.cookies.get("session_token")
        if not token:
            return await call_next(request)

        template = _matched_template(request)
        # Static files render nothing role-dependent; skip the session lookup.
        if template is not None and template.startswith("/static/"):
            return await call_next(request)

        try:
            conn = get_db()
        except RuntimeError:
            return await call_next(request)
        username = resolve_cached_session(request, conn, token)
        if username is None:
            return await call_next(request)

        role = "admin" if get_user_role(conn, username) == "admin" else "viewer"
        request.state.username = username
        request.state.user_role = role

        if role == "admin" or _viewer_may_reach(request.method, template):
            return await call_next(request)

        path = request.url.path
        if request.method == "GET" and not (path == "/api" or path.startswith("/api/")):
            return RedirectResponse("/watch", status_code=303)
        return JSONResponse({"error": "forbidden"}, status_code=403)
