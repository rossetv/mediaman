from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from mediaman.core.format import format_bytes, title_from_audit_detail
from mediaman.db import get_db
from mediaman.scanner.repository.audit import (
    AuditRow,
    count_audit_rows,
    fetch_security_audit_rows,
)
from mediaman.web.auth.middleware import get_current_admin, get_current_user, resolve_page_session
from mediaman.web.history_text import (
    DELETE_ROOT_PREFIX,
    event_title,
    hide_emails,
    redact_text,
    scrub_control,
)
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SCHEDULED_DELETION, ACTION_SNOOZED
from mediaman.web.repository import history as repo
from mediaman.web.repository.avatars import avatar_url
from mediaman.web.responses import respond_err
from mediaman.web.routes._history_view import (
    describe,
    own_item,
    play_item,
    shown_name,
    split_security_body,
    with_who,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_PER_PAGE_DEFAULT = 25
_PER_PAGE_MAX = 100
# Its offset fits what SQLite can hold.
_PAGE_MAX = 10**9

ACTION_TYPES = [
    "scheduled",
    ACTION_SNOOZED,
    "kept",
    "deleted",
    "delete_failed",
    "downloaded",
    "re_downloaded",
    "unkept",
    "kept_show",
    "removed_show_keep",
    "security",
    *repo.GROUPS,
]

# The groups offered, by name. The other filters above stay for old links.
_GROUP_LABELS = {
    repo.PLAYS: "Plays",
    "downloads": "Downloads",
    "keeps": "Keeps",
    "deletions": "Deletions",
    "sign_ins": "Sign-ins",
    "people": "People",
    "settings": "Settings",
    "scans": "Scans",
    "newsletter": "Newsletter",
}

_FILTER_NAMES = ("q", "user", "action", "title", "since", "until")

# The group that lights for an old link's filter; "security" spans several.
_GROUP_COVERING = {
    "scheduled": "deletions",
    ACTION_SNOOZED: "keeps",
    "kept": "keeps",
    "deleted": "deletions",
    "delete_failed": "deletions",
    "downloaded": "downloads",
    "re_downloaded": "downloads",
    "unkept": "keeps",
    "kept_show": "keeps",
    "removed_show_keep": "keeps",
}

ACTION_BADGE_CLASS = {
    "scanned": "badge-action-scanned",
    "scheduled": "badge-action-scheduled",
    ACTION_SCHEDULED_DELETION: "badge-action-scheduled",
    ACTION_SNOOZED: "badge-action-protected",
    "kept": "badge-action-protected",
    "protected": "badge-action-protected",
    ACTION_PROTECTED_FOREVER: "badge-action-protected",
    "deleted": "badge-action-deleted",
    "delete_failed": "badge-action-deleted",
    "downloaded": "badge-action-downloaded",
    "re_downloaded": "badge-action-redownloaded",
    "unkept": "badge-action-unprotected",
    "unprotected": "badge-action-unprotected",
    "kept_show": "badge-action-protected",
    "removed_show_keep": "badge-action-unprotected",
    "dry_run_skip": "badge-action-scanned",
    "delete_failed_permanent": "badge-action-deleted",
    "delete_refused": "badge-action-deleted",
    "delete_root_unavailable": "badge-action-deleted",
    "delete_root_available": "badge-action-scanned",
    "delete_incomplete": "badge-action-deleted",
    "delete_folders_failed": "badge-action-deleted",
    "delete_awaiting_scan": "badge-action-scanned",
    "media_missing": "badge-action-scanned",
    "unmonitor_skipped": "badge-action-scanned",
    "library_left_out": "badge-action-deleted",
}

ACTION_LABELS = {
    # A snooze is recorded when an admin or viewer keeps an item for a while.
    ACTION_SNOOZED: "kept",
    "protected": "kept",
    ACTION_PROTECTED_FOREVER: "kept",
    "unprotected": "keep removed",
    "unkept": "keep removed",
    ACTION_SCHEDULED_DELETION: "scheduled",
    "kept_show": "kept show",
    "removed_show_keep": "keep removed",
    "dry_run_skip": "dry run: skipped",
    "delete_failed": "delete failed",
    "delete_failed_permanent": "delete failed",
    "delete_refused": "delete refused",
    "media_missing": "media missing",
    "unmonitor_skipped": "unmonitor skipped",
    "library_left_out": "library not scanned",
    "delete_root_unavailable": "delete root unavailable",
    "delete_root_available": "delete root available",
    "delete_awaiting_scan": "waiting",
    "delete_incomplete": "delete incomplete",
    "delete_folders_failed": "folder cleanup failed",
    "re_downloaded": "re-downloaded",
}

# What a waiting deletion waits for, by the first of these its stored text has.
_WAIT_LABELS = (
    ("its folder also holds", "waiting for folder"),
    ("library Plex does not list", "waiting for library"),
    ("disk limit in Settings", "waiting for disk limit"),
    ("library is not selected", "library not selected"),
    ("while that one is kept", "waiting for keep to end"),
    ("until that watch", "waiting for watch to age"),
    ("scan to record", "waiting for scan"),
)

SECURITY_EVENT_LABELS = {
    "login.success": "Signed in",
    "login.failed": "Sign-in failed",
    "logout": "Signed out",
    "user.created": "User created",
    "user.deleted": "User deleted",
    "user.role_changed": "User role changed",
    "user.email_updated": "User email changed",
    "user.unlocked": "User unlocked",
    "password.changed": "Password changed",
    "password.force_changed": "Password changed",
    "reauth.granted": "Password confirmed",
    "reauth.failed": "Password confirmation failed",
    "session.destroy": "Session ended",
    "session.revoke_one": "Device signed out",
    "session.revoke_all": "All sessions ended",
    "settings.write": "Settings changed",
    "settings.write.throttled": "Settings changes throttled",
    "scan.triggered": "Scan started",
    "scan.cleared": "Scan cleared",
    "library.sync": "Library synced",
    "library.sync.failed": "Library sync failed",
    "newsletter.sent": "Newsletter sent",
    "subscriber.added": "Subscriber added",
    "subscriber.removed": "Subscriber removed",
    "subscriber.opted_out": "Subscriber opted out",
    "aes.canary_failed": "Encryption check failed",
    "auto_abandon.fired": "Download abandoned automatically",
    "password.change_failed": "Password change failed",
    "password.force_change_failed": "Password change failed",
    "password.weak_detected": "Weak password detected",
    "user.delete.rate_limited": "User deletion throttled",
    "user.email_update.rate_limited": "User email change throttled",
}

# The library a scan left out, whose name may hold a quote or a slash.
_LEFT_OUT_RE = re.compile(r"Plex library '(.*)' was not scanned", re.S)


def _security_label(event: str) -> str:
    return (
        SECURITY_EVENT_LABELS.get(event) or event.replace(".", " ").replace("_", " ").capitalize()
    )


def _security_detail(detail: str | None, actor: str | None) -> str:
    """Return "who from where" for a security row; the raw body is never shown."""
    who, ip, _ = split_security_body(scrub_control(detail), actor)
    return " from ".join(p for p in (who, ip) if p)


def _build_item(r: AuditRow) -> dict[str, object]:
    action = r.action
    is_security = isinstance(action, str) and action.startswith("sec:")

    if is_security:
        title = line_title = _security_label(action[4:])
        action_label = "security"
        badge = "badge-action-security"
    else:
        resolved_title = r.mi_title or r.ks_title
        line_title = resolved_title or event_title(action, r.media_item_id, r.detail)
        if not resolved_title and r.detail:
            resolved_title = title_from_audit_detail(r.detail, strict=True)
            if resolved_title == "Unknown":
                m = re.search(r"'([^'/\\][^']*)'", r.detail)
                resolved_title = redact_text(m.group(1)) if m else resolved_title
        title = resolved_title or "Unknown"
        line_title = line_title or "Unknown"
        if r.media_item_id.startswith(DELETE_ROOT_PREFIX):
            title = r.media_item_id[len(DELETE_ROOT_PREFIX) :]
            line_title = "Delete folder"
        elif action == "library_left_out" and (library := _LEFT_OUT_RE.match(r.detail or "")):
            title = redact_text(scrub_control(library.group(1)))
            line_title = hide_emails(title)
        action_label = ACTION_LABELS.get(action, action)
        if action == "delete_awaiting_scan":
            waits = (label for words, label in _WAIT_LABELS if words in (r.detail or ""))
            action_label = next(waits, action_label)
        badge = ACTION_BADGE_CLASS.get(action, "badge-action-scanned")

    space_impact = format_bytes(r.space_reclaimed_bytes) if r.space_reclaimed_bytes else ""
    view = describe(
        event_id=r.id,
        action=action,
        action_label=_security_label(action[4:]) if is_security else action_label,
        detail=r.detail,
        actor=r.actor,
        created_at=r.created_at,
        is_security=is_security,
        title=title,
        media_item_id=r.media_item_id,
        plex_rating_key=r.plex_rating_key,
        space_impact=space_impact,
    )
    return {
        "id": r.id,
        "media_item_id": r.media_item_id,
        "action": action,
        "action_label": action_label,
        "badge_class": badge,
        "detail": _security_detail(r.detail, r.actor)
        if is_security
        else redact_text(scrub_control(r.detail)),
        "space_impact": space_impact,
        "created_at": r.created_at,
        "title": title,
        "line_title": line_title,
        "plex_rating_key": r.plex_rating_key,
        "is_security": is_security,
        "summary": view.summary,
        "actor": view.actor,
        "ip": view.ip,
        "fields": view.fields,
    }


def _fetch_security_events(
    conn: sqlite3.Connection, page: int, per_page: int
) -> tuple[list[dict[str, object]], int]:
    rows = fetch_security_audit_rows(conn, page=page, per_page=per_page)
    return [_build_item(r) for r in rows], count_audit_rows(conn, "security")


@dataclass(frozen=True, slots=True)
class _Listing:
    """A numbered page: 1-based, or past the last one and empty."""

    page: int
    total: int
    entries: list[repo.Entry]
    next_cursor: str | None
    #: The event asked for, when it is on this page.
    open_id: int | None = None


def _pages(total: int, per_page: int) -> int:
    return max(1, (total + per_page - 1) // per_page)


def _read_page(
    conn: sqlite3.Connection, viewer: repo.Viewer, filters: repo.Filters, page: int, per_page: int
) -> _Listing:
    found = repo.history_page(conn, viewer, filters, per_page=per_page)
    total = found.total or 0
    if page == 1:
        return _Listing(1, total, found.entries, found.next_cursor)
    cursor = found.next_cursor if page <= _pages(total, per_page) else None
    skip = (page - 2) * per_page
    while cursor and skip:
        step = min(skip, repo.PER_PAGE_MAX)
        cursor = repo.history_page(conn, viewer, filters, cursor=cursor, per_page=step).next_cursor
        skip -= step
    if cursor is None:
        return _Listing(page, total, [], None)
    found = repo.history_page(conn, viewer, filters, cursor=cursor, per_page=per_page)
    return _Listing(page, total, found.entries, found.next_cursor)


def _page_of_event(
    conn: sqlite3.Connection,
    viewer: repo.Viewer,
    filters: repo.Filters,
    event_id: int,
    per_page: int,
) -> int | None:
    """The page that holds *event_id*; None when *viewer* may not see it or a filter hides it."""
    event = repo.event_for(conn, viewer, event_id)
    if event is None:
        return None
    since = repo.parse_filters(since=event.created_at).since
    if since is None:
        return None
    from_then = replace(filters, since=since)
    before = repo.history_page(conn, viewer, from_then, per_page=1).total or 0
    # Less what shares the event's moment and is listed from it on.
    cursor: str | None = repo.cursor_of(event)
    while cursor:
        rest = repo.history_page(conn, viewer, from_then, cursor=cursor, per_page=repo.PER_PAGE_MAX)
        before -= len(rest.entries)
        cursor = rest.next_cursor
    return before // per_page + 1


def _read(
    conn: sqlite3.Connection,
    viewer: repo.Viewer,
    filters: repo.Filters,
    *,
    page: int,
    per_page: int,
    event: str,
) -> _Listing:
    """The page asked for, or the one that holds *event* when there is such a one."""
    event_id = int(event) if event.isdecimal() and len(event) < 19 else None
    held = event_id and _page_of_event(conn, viewer, filters, event_id, per_page)
    if held:
        listing = _read_page(conn, viewer, filters, held, per_page)
        if any(not isinstance(e, repo.Play) and e.id == event_id for e in listing.entries):
            return replace(listing, open_id=event_id)
    return _read_page(conn, viewer, filters, page, per_page)


def _filters(
    viewer: repo.Viewer, *, q: str, user: str, action: str, title: str, since: str, until: str
) -> repo.Filters:
    """An unknown action is no filter, and a name is one for an admin only."""
    return repo.parse_filters(
        q=q,
        user=user if viewer.is_admin else "",
        action=action if action in ACTION_TYPES else "",
        title=title,
        since=since,
        until=until,
    )


def _avatars(conn: sqlite3.Connection, viewer: repo.Viewer) -> dict[str, str | None]:
    """The avatar of each account *viewer* may see, by name."""
    return {name: avatar_url(conn, name) for name in repo.account_names(conn, viewer)}


def _person(who: repo.Who, avatars: dict[str, str | None]) -> dict[str, object]:
    return {
        "value": who.value,
        "username": shown_name(who),
        "kind": who.kind,
        "avatar": avatars.get(who.name) if who.kind == "account" else None,
    }


def _items(
    conn: sqlite3.Connection, viewer: repo.Viewer, entries: Sequence[repo.Entry]
) -> list[dict[str, object]]:
    avatars = _avatars(conn, viewer)
    whose = repo.whose(conn, entries)
    items = []
    for entry in entries:
        if isinstance(entry, repo.Play):
            item, who = play_item(entry), whose[entry.ref]
        else:
            item, who = _build_item(entry), whose[entry.id]
            if not viewer.is_admin:
                item = own_item(item, viewer.username)
        avatar = avatars.get(who.name) if who.kind == "account" else None
        items.append({**with_who(item, who), "avatar": avatar})
    return items


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> Response:
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved
    viewer = repo.viewer_for(conn, username)
    if viewer is None:
        return RedirectResponse("/login", status_code=302)

    params = request.query_params
    filters = _filters(viewer, **{k: params.get(k, "") for k in _FILTER_NAMES})
    try:
        page = min(max(1, int(params.get("page", 1))), _PAGE_MAX)
    except ValueError:
        page = 1

    per_page = _PER_PAGE_DEFAULT
    event = params.get("event", "")
    listing = _read(conn, viewer, filters, page=page, per_page=per_page, event=event)
    total_pages = _pages(listing.total, per_page)
    if listing.page > total_pages:
        listing = _read_page(conn, viewer, filters, total_pages, per_page)

    avatars = _avatars(conn, viewer)
    action = filters.action
    templates = cast(Jinja2Templates, request.app.state.templates)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "username": username,
            "nav_active": "history",
            "is_admin": viewer.is_admin,
            "items": _items(conn, viewer, listing.entries),
            "people": [_person(who, avatars) for who in repo.people(conn, viewer)],
            "open_id": listing.open_id,
            "action_types": [
                (name, label)
                for name, label in _GROUP_LABELS.items()
                if viewer.is_admin or name in repo.OWN_GROUPS
            ],
            "filters": {k: params.get(k, "") for k in ("since", "until")}
            | {"q": filters.q, "user": filters.user, "action": action, "title": filters.title},
            "action_filter": action,
            "pill_on": _GROUP_COVERING.get(action, action),
            "filter_label": _GROUP_LABELS.get(
                action, ACTION_LABELS.get(action, action.replace("_", " "))
            ),
            "page": listing.page,
            "per_page": per_page,
            "total": listing.total,
            "total_pages": total_pages,
            "next_cursor": listing.next_cursor,
        },
    )


@router.get("/api/history")
def api_history(
    q: str = "",
    user: str = "",
    action: str = "",
    title: str = "",
    since: str = "",
    until: str = "",
    cursor: str = "",
    event: str = "",
    page: int = Query(default=1, ge=1, le=_PAGE_MAX),
    per_page: int = Query(default=_PER_PAGE_DEFAULT, ge=1, le=_PER_PAGE_MAX),
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """What this person may see, newest first: by *page*, the page of *event*, or what follows
    *cursor*, where nothing is counted."""
    conn = get_db()
    viewer = repo.viewer_for(conn, username)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    filters = _filters(viewer, q=q, user=user, action=action, title=title, since=since, until=until)
    if cursor:
        try:
            after = repo.history_page(conn, viewer, filters, cursor=cursor, per_page=per_page)
        except repo.BadCursor:
            return respond_err("bad_cursor", status=400)
        entries, next_cursor = after.entries, after.next_cursor
        place: dict[str, object] = dict.fromkeys(("page", "total", "total_pages", "open_id"))
    else:
        listing = _read(conn, viewer, filters, page=page, per_page=per_page, event=event)
        entries, next_cursor = listing.entries, listing.next_cursor
        place = {
            "page": listing.page,
            "total": listing.total,
            "total_pages": _pages(listing.total, per_page),
            "open_id": listing.open_id,
        }
    return JSONResponse(
        {
            "items": _items(conn, viewer, entries),
            "per_page": per_page,
            "next_cursor": next_cursor,
            **place,
        }
    )


@router.get("/api/history/menu")
def api_history_menu(
    menu: Literal["user", "action", "title"],
    find: str = "",
    q: str = "",
    user: str = "",
    action: str = "",
    title: str = "",
    since: str = "",
    until: str = "",
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The choices of one filter's menu, each with how many rows it would show while the other
    filters stay as they are. *find* narrows the titles."""
    conn = get_db()
    viewer = repo.viewer_for(conn, username)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    filters = _filters(viewer, q=q, user=user, action=action, title=title, since=since, until=until)
    if menu == "user":
        avatars = _avatars(conn, viewer)
        options: list[dict[str, object]] = [
            {**_person(who, avatars), "count": n}
            for who, n in repo.user_counts(conn, viewer, filters)
        ]
    elif menu == "action":
        options = [
            {"value": name, "label": _GROUP_LABELS[name], "count": n}
            for name, n in repo.action_counts(conn, viewer, filters).items()
        ]
    else:
        options = [
            {"value": t.title, "label": t.title, "count": t.count}
            for t in repo.title_counts(conn, viewer, filters, find)
        ]
    more = menu == "title" and len(options) == repo.TITLES_MAX
    return JSONResponse({"menu": menu, "options": options, "more": more})


@router.get("/api/security-events")
def api_security_events(
    page: int = Query(default=1, ge=1, le=_PAGE_MAX),
    per_page: int = Query(default=_PER_PAGE_DEFAULT, ge=1, le=_PER_PAGE_MAX),
    _admin: str = Depends(get_current_admin),
) -> JSONResponse:
    conn = get_db()
    items, total = _fetch_security_events(conn, page, per_page)
    return JSONResponse(
        {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": _pages(total, per_page),
        }
    )
