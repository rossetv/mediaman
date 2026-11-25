"""Every route of the real app either refuses a viewer or is meant for one.

The walk covers ``app.routes`` as ``create_app`` builds it, so a route
added later is refused to viewers unless it is put on the list below.
"""

from __future__ import annotations

import re

from fastapi.routing import iter_route_contexts

#: What a viewer may reach, written out independently of the guard.
_VIEWER_MAY_REACH = {
    ("GET", "/watch"),
    ("GET", "/watch/show/{show_key}"),
    ("GET", "/watch/movie/{key}"),
    ("GET", "/watch/play/{rating_key}"),
    ("GET", "/api/watch/search"),
    ("POST", "/api/watch/keep"),
    ("POST", "/api/watch/unkeep"),
    ("GET", "/api/watch/thumb/{rating_key}"),
    ("GET", "/api/watch/img/tmdb/{size}/{name}"),
    ("GET", "/api/watch/search/tmdb"),
    ("GET", "/api/watch/discover"),
    ("GET", "/api/watch/title/{ref}"),
    ("GET", "/api/watch/title/{ref}/ratings"),
    ("GET", "/api/watch/title/{ref}/season/{number}"),
    ("GET", "/api/watch/recommended"),
    ("POST", "/api/watch/stream"),
    ("GET", "/api/watch/stream/{stream_id}/index.m3u8"),
    ("GET", "/api/watch/stream/{stream_id}/seg/{name}"),
    ("POST", "/api/watch/stream/{stream_id}/stop"),
    ("POST", "/api/watch/progress"),
    ("GET", "/api/watch/stream/{stream_id}/preview/{n}"),
    ("POST", "/api/watch/dismissed/{ref}"),
    ("DELETE", "/api/watch/dismissed/{ref}"),
    ("POST", "/api/watch/watched/{kind}/{key}"),
    ("DELETE", "/api/watch/watched/{kind}/{key}"),
    ("GET", "/api/watch/library/{kind}/row"),
    ("POST", "/api/watch/picks/refresh"),
    ("GET", "/api/watch/picks/refresh"),
    ("POST", "/api/watch/picks/{pick_id}/download"),
    ("GET", "/api/watch/explore"),
    ("GET", "/api/watch/person/{person_id}"),
    ("GET", "/api/watch/preview/{ref}"),
    ("GET", "/api/watch/ratings"),
    ("PUT", "/api/watch/ratings/{ref}"),
    ("DELETE", "/api/watch/ratings/{ref}"),
    ("POST", "/api/watch/ratings/ask/{rating_key}"),
    ("GET", "/search"),
    ("GET", "/api/search"),
    ("GET", "/api/search/discover"),
    ("GET", "/api/search/detail/{media_type}/{tmdb_id}"),
    ("POST", "/api/search/download"),
    ("GET", "/api/search/assist"),
    ("POST", "/api/search/assist/searches"),
    ("DELETE", "/api/search/assist/searches"),
    ("GET", "/api/search/assist/history"),
    ("PUT", "/api/search/assist/history"),
    ("POST", "/api/search/assist/ask"),
    ("POST", "/api/search/assist/like"),
    ("GET", "/api/search/assist/runs/{run_id}"),
    ("DELETE", "/api/search/assist/runs/{run_id}"),
    ("GET", "/api/search/assist/tonight"),
    ("GET", "/downloads"),
    ("GET", "/api/downloads"),
    ("GET", "/api/download/status"),
    ("GET", "/recommended"),
    ("GET", "/api/recommended"),
    ("POST", "/api/recommended/{recommendation_id}/download"),
    ("GET", "/api/poster/{rating_key}"),
    ("GET", "/account"),
    ("GET", "/api/account/plex"),
    ("POST", "/api/account/plex"),
    ("DELETE", "/api/account/plex"),
    ("POST", "/api/account/plex/check"),
    ("POST", "/api/account/plex/cancel"),
    ("DELETE", "/api/account/plex/notice"),
    ("GET", "/api/account/avatar"),
    ("PUT", "/api/account/avatar"),
    ("DELETE", "/api/account/avatar"),
    ("PUT", "/api/account/avatar/photo"),
    ("GET", "/api/avatars/{user_id}"),
    ("POST", "/api/users/change-password"),
    ("PATCH", "/api/users/me/email"),
    ("GET", "/api/users/sessions"),
    ("POST", "/api/users/sessions/revoke-others"),
    ("DELETE", "/api/users/sessions/{public_id}"),
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
_VIEWER_PREFIXES = ("/static/",)

_ORIGIN = {"origin": "http://testserver"}


def _viewer_may_reach(method: str, template: str) -> bool:
    return (method, template) in _VIEWER_MAY_REACH or template.startswith(_VIEWER_PREFIXES)


def _routes(app) -> list[tuple[str, str]]:
    """Every ``(method, path template)`` the app serves; a mount counts as GET."""
    pairs = []
    for route in iter_route_contexts(app.routes):
        for method in sorted(route.methods or {"GET"}):
            pairs.append((method, route.path_format))
    return pairs


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", template)


def test_every_route_refuses_a_viewer_unless_it_is_meant_for_one(guarded_app, conn, viewer_client):
    app, reached = guarded_app
    client = viewer_client(app, conn)
    routes = _routes(app)
    assert ("GET", "/settings") in routes
    assert ("POST", "/api/downloads/{dl_id}/abandon") in routes

    wrong = []
    for method, template in routes:
        path = _concrete(template)
        reached.clear()
        resp = client.request(method, path, headers=_ORIGIN, follow_redirects=False)
        if _viewer_may_reach(method, template):
            if resp.status_code != 204 or not reached:
                wrong.append((method, template, "blocked", resp.status_code))
            continue
        if reached:
            wrong.append((method, template, "handler reached", resp.status_code))
        elif method == "GET" and not template.startswith("/api/"):
            if (resp.status_code, resp.headers.get("location")) != (303, "/watch"):
                wrong.append((method, template, "not redirected", resp.status_code))
        elif (resp.status_code, resp.json()) != (403, {"error": "forbidden"}):
            wrong.append((method, template, "not 403", resp.status_code))

    assert wrong == []


def test_an_admin_reaches_every_route(guarded_app, conn, authed_client):
    app, reached = guarded_app
    client = authed_client(app, conn)
    routes = _routes(app)

    blocked = []
    for method, template in routes:
        reached.clear()
        resp = client.request(method, _concrete(template), headers=_ORIGIN, follow_redirects=False)
        if resp.status_code != 204 or not reached:
            blocked.append((method, template, resp.status_code))

    assert blocked == []
