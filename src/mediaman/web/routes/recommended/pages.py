"""HTML page handlers for /recommended."""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date as _date
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.time import now_utc
from mediaman.services.arr.state import LazyArrClients, attach_download_states
from mediaman.services.infra import get_bool_setting
from mediaman.services.openai.recommendations.throttle import refresh_cooldown_remaining
from mediaman.web.auth.middleware import resolve_page_session
from mediaman.web.repository.recommended import fetch_recommendations

router = APIRouter()


def _relative_label(batch_date: _date | None, index: int, today: _date) -> str:
    """Return a human-friendly label for a recommendation batch date."""
    if index == 0:
        return "Latest picks"
    if batch_date is None:
        return "Earlier picks"
    days = (today - batch_date).days
    if days <= 0:
        return "Earlier today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "Last week"
    weeks = days // 7
    if weeks < 5:
        return f"{weeks} weeks ago"
    months = max(1, days // 30)
    return "A month ago" if months == 1 else f"{months} months ago"


#: Maximum number of recommendation batches surfaced on the page.
#: Older batches stay in the database — the user can still navigate to
#: them via the API or a future "show more" affordance — but the page
#: itself only renders the four most recent so the initial paint stays
#: bounded — older batches remain in the DB but are not rendered on the initial page load.
_MAX_VISIBLE_BATCHES = 4


def _group_into_batches(
    recommendations: list[dict[str, object]],
    today: _date,
) -> tuple[list[dict[str, object]], int]:
    """Group recommendations by ``batch_id``, preserving DESC order.

    Returns ``(formatted, total_batches)`` — the page renders at most
    :data:`_MAX_VISIBLE_BATCHES` of those, but the caller can use the
    full count to surface a "showing 4 of N" affordance and avoid
    silently hiding older picks.
    """
    batches_map: OrderedDict[str, dict[str, list[dict[str, object]]]] = OrderedDict()
    for s in recommendations:
        created_at = s.get("created_at", "")
        created_prefix = created_at[:10] if isinstance(created_at, str) else ""
        bid_raw = s.get("batch_id") or created_prefix
        bid = str(bid_raw) if bid_raw else ""
        if bid not in batches_map:
            batches_map[bid] = {"trending": [], "personal": []}
        if s.get("category") == "trending":
            batches_map[bid]["trending"].append(s)
        else:
            batches_map[bid]["personal"].append(s)

    total_batches = len(batches_map)
    formatted_batches: list[dict[str, object]] = []
    for index, (bid, groups) in enumerate(list(batches_map.items())[:_MAX_VISIBLE_BATCHES]):
        batch_date: _date | None
        try:
            batch_date = datetime.strptime(str(bid), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            batch_date = None
            date_label = str(bid)
        else:
            date_label = batch_date.strftime("%-d %B %Y")
        formatted_batches.append(
            {
                "batch_id": bid,
                "date_label": date_label,
                "relative_label": _relative_label(batch_date, index, today),
                "is_latest": index == 0,
                "trending": groups["trending"],
                "personal": groups["personal"],
            }
        )
    return formatted_batches, total_batches


def _json_safe(value: object) -> object:
    """Coerce a single value to a JSON-serialisable form.

    Replaces the catch-all ``default=str`` previously passed to
    :func:`json.dumps`, which silently stringified anything it didn't
    recognise — including types like ``bytes`` that produced incorrect
    output (``b'...'`` literals leaking into the embedded JSON
    payload). The handler now refuses unknown types loudly, which is
    what we want: we control every field that lands in
    ``all_recommendations_json`` and any new type showing up should be
    spotted, not papered over.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    raise TypeError(
        f"recommended/pages: unexpected non-JSON type {type(value).__name__} "
        f"in recommendation payload — extend _json_safe before adding "
        f"this type to a recommendation field"
    )


@router.get("/recommended", response_class=HTMLResponse)
def recommended_page(request: Request) -> Response:
    """Render the Recommended For You page, grouping recommendations by batch into accordion sections."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    enabled = get_bool_setting(conn, "suggestions_enabled", default=True)
    recommendations = fetch_recommendations(conn) if enabled else []

    today = _date.today()
    formatted_batches, total_batches = _group_into_batches(recommendations, today)
    older_batches_count = max(0, total_batches - len(formatted_batches))

    # Check library state for downloaded items.
    # Share URLs are no longer embedded in the page — they are minted on
    # demand when the user clicks the share button, via
    # POST /api/recommended/{id}/share-token.
    config = request.app.state.config

    arr = LazyArrClients(conn, config.secret_key)
    all_recs = attach_download_states(formatted_batches, arr)

    # Use an explicit type whitelist instead of ``default=str`` so an
    # unexpected non-JSON value crashes the handler loudly rather than
    # silently rendering ``str(value)`` into the page.
    all_recommendations_json = json.dumps(
        {str(k): _json_safe(v) for k, v in all_recs.items()},
    ).replace("</", "<\\/")

    cooldown = refresh_cooldown_remaining(conn)
    if cooldown is None:
        manual_refresh_available = True
        next_manual_refresh_at = None
    else:
        manual_refresh_available = False
        next_manual_refresh_at = (now_utc() + cooldown).isoformat()

    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "recommended.html",
        {
            "username": username,
            "nav_active": "recommended",
            "batches": formatted_batches,
            "enabled": enabled,
            "all_recommendations_json": all_recommendations_json,
            "manual_refresh_available": manual_refresh_available,
            "next_manual_refresh_at": next_manual_refresh_at,
            # Surface the count so the template can render an "Older
            # picks not shown" hint when needed. Templates
            # may render this conditionally on ``older_batches_count >
            # 0``; templates not yet updated simply ignore the field.
            "older_batches_count": older_batches_count,
            "max_visible_batches": _MAX_VISIBLE_BATCHES,
        },
    )
