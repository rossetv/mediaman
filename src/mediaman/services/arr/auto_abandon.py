"""Auto-abandon escalation policy for over-searched monitored items.

When a monitored Radarr/Sonarr item has been searched ``escalate_at *
multiplier`` times without ever matching an NZB, the operator's
configured policy may unmonitor it automatically. This module owns
:func:`maybe_auto_abandon` and the per-fire ``sec:auto_abandon.fired``
audit emission that makes a compromised-settings attack discoverable
after the fact.

Split out of :mod:`mediaman.services.arr.search_trigger` so the policy
logic and its audit guarantees are isolated from the trigger-decision
state machine. :func:`maybe_auto_abandon` is re-exported from
:mod:`mediaman.services.arr.search_trigger` for backwards compatibility.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.audit import security_event
from mediaman.services.infra.settings_reader import get_int_setting

logger = logging.getLogger("mediaman")


def maybe_auto_abandon(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    item: dict,
    search_count: int,
) -> None:
    """Auto-unmonitor *item* if its search count has crossed the threshold.

    Multiplier of 0 (default) disables the feature; the function returns
    immediately. Otherwise abandons via the same service entry-points the
    manual button uses, so semantics (throttle clear, partial-failure
    behaviour, logging) are identical.

    Series with no derivable season list (no episodes in the queue) are
    skipped — there's nothing for Sonarr to unmonitor that wouldn't be a
    no-op or an error.
    """
    multiplier = get_int_setting(conn, "abandon_search_auto_multiplier", default=0, min=0, max=100)
    if multiplier <= 0:
        return
    escalate_at = get_int_setting(conn, "abandon_search_escalate_at", default=50, min=2, max=10000)
    if search_count < escalate_at * multiplier:
        return

    # Late import breaks the otherwise-circular dependency between
    # auto_abandon and the abandon service (which itself imports
    # clear_throttle from the throttle module).
    from mediaman.services.downloads.abandon import (
        abandon_movie,
        abandon_seasons,
    )

    dl_id = item.get("dl_id") or ""
    arr_id = item.get("arr_id") or 0
    if not dl_id or not arr_id:
        return

    kind = item.get("kind")
    if kind == "movie":
        # Audit BEFORE the abandon call so the trail records the policy
        # firing even if Radarr is down. A settings-write attacker who
        # sets multiplier=1 to mass-unmonitor every item leaves one
        # ``sec:auto_abandon.fired`` row per affected item — discoverable
        # by an operator scanning the audit log. Pass ``actor=""`` to
        # mark this as a system-driven (not admin-triggered) event.
        security_event(
            conn,
            event="auto_abandon.fired",
            actor="",
            ip="",
            detail={
                "dl_id": dl_id,
                "arr_id": arr_id,
                "service": "radarr",
                "kind": "movie",
                "multiplier": multiplier,
                "escalate_at": escalate_at,
                "search_count": search_count,
            },
        )
        abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
        return

    # Filter season 0 (specials): Sonarr uses S00 for specials, and
    # ``abandon_seasons`` would otherwise unmonitor every special when
    # all queue rows happen to be specials (Domain-06 #12). Specials
    # are typically opt-in monitored separately — we never want to
    # auto-unmonitor them.
    seasons = sorted(
        {
            int(ep.get("season_number") or 0)
            for ep in (item.get("episodes") or [])
            if int(ep.get("season_number") or 0) > 0
        }
    )
    if not seasons:
        return
    security_event(
        conn,
        event="auto_abandon.fired",
        actor="",
        ip="",
        detail={
            "dl_id": dl_id,
            "arr_id": arr_id,
            "service": "sonarr",
            "kind": "series",
            "seasons": seasons,
            "multiplier": multiplier,
            "escalate_at": escalate_at,
            "search_count": search_count,
        },
    )
    abandon_seasons(
        conn,
        secret_key,
        series_id=arr_id,
        season_numbers=seasons,
        dl_id=dl_id,
    )
