"""Auto-abandon escalation policy for long-stalled monitored items.

When a monitored Radarr/Sonarr item has been sitting in `searching` state
beyond ``_AUTO_ABANDON_AFTER_SECONDS`` (14 days), and the operator has
enabled the ``auto_abandon_enabled`` setting, this module unmonitors it
and emits a ``sec:auto_abandon.fired`` audit row. The audit row makes a
compromised-settings attack discoverable after the fact.

Split out of :mod:`mediaman.services.arr.search_trigger` so the policy
logic and its audit guarantees are isolated from the trigger-decision
state machine. :func:`maybe_auto_abandon` is re-exported from
:mod:`mediaman.services.arr.search_trigger` for backwards compatibility.
"""

from __future__ import annotations

import logging
import sqlite3

from mediaman.audit import security_event
from mediaman.services.infra.settings_reader import get_bool_setting

logger = logging.getLogger(__name__)

# How long an item has to sit in `searching` state before the manual Abandon
# button appears in the queue UI. Time-based (clock = item.added_at) so the
# threshold is independent of how often the page is polled.
_ABANDON_BUTTON_VISIBLE_AFTER_SECONDS = 10 * 3600  # 10 h

# When auto-abandon is enabled in settings, items older than this since
# added_at get unmonitored automatically.
_AUTO_ABANDON_AFTER_SECONDS = 14 * 86_400  # 14 d

# Refuse to abandon anything whose release date isn't at least this far in
# the past. Coming-soon items naturally have no copies on indexers; movies
# released a week ago might still be propagating. Only items released
# meaningfully long ago should ever count as "stuck" rather than "fresh".
_AUTO_ABANDON_RELEASE_GRACE_SECONDS = 30 * 86_400  # 30 d


def maybe_auto_abandon(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    item: dict,
    now: float,
) -> None:
    """Auto-unmonitor *item* if it has been searching beyond the time threshold.

    Driven by the boolean ``auto_abandon_enabled`` setting (default off).
    Time clock is ``item["added_at"]``, matching the manual Abandon button's
    visibility threshold. Series with no positive-numbered seasons in the
    queue are skipped — Sonarr only knows about specials there, and we
    never want to auto-unmonitor specials.

    Release-date guard: items the user can see in the "Coming soon" section
    (``is_upcoming``) and items whose release is too recent to plausibly
    have stalled (``released_at`` within
    :data:`_AUTO_ABANDON_RELEASE_GRACE_SECONDS`) are never abandoned. When
    the release date is unknown (``released_at`` is 0) we also skip — we'd
    rather leave a search running than mistakenly bin a coming-soon item
    whose Radarr/Sonarr metadata simply hasn't filled in dates yet.

    Abandons via the same service entry-points the manual button uses, so
    semantics (throttle clear, partial-failure behaviour, logging) are
    identical.
    """
    if not get_bool_setting(conn, "auto_abandon_enabled", default=False):
        return
    if item.get("is_upcoming"):
        # Movies/series the user sees under "Coming soon" — Radarr/Sonarr
        # correctly won't find indexer matches yet, and abandoning them
        # would silently unmonitor something the user is actively waiting
        # for.
        return
    released_at = item.get("released_at") or 0.0
    if released_at <= 0.0:
        # Release date unknown — be conservative and never abandon. The
        # alternative (assuming "released long ago") would mistakenly
        # unmonitor coming-soon entries whose dates haven't been filled
        # in by metadata providers yet.
        return
    if now - released_at < _AUTO_ABANDON_RELEASE_GRACE_SECONDS:
        # Too fresh: NZBs/torrents may still be propagating. Auto-abandon
        # is meant to clean up genuinely stuck items, not bin recent
        # releases that just haven't surfaced yet.
        return
    added_at = item.get("added_at") or 0.0
    if added_at <= 0.0:
        # No reliable timestamp — treat as unknown age and skip rather than
        # immediately abandoning (now - 0.0 ≈ 1.7e9 s, way past any threshold).
        return
    if now - added_at < _AUTO_ABANDON_AFTER_SECONDS:
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

    searching_for_seconds = int(now - added_at)
    kind = item.get("kind")
    if kind == "movie":
        # Audit BEFORE the abandon call so the trail records the policy
        # firing even if Radarr is down. Pass actor="" so the row is
        # marked as a system-driven (not admin-triggered) event.
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
                "searching_for_seconds": searching_for_seconds,
            },
        )
        abandon_movie(conn, secret_key, arr_id=arr_id, dl_id=dl_id)
        return

    # Filter season 0 (specials): Sonarr uses S00 for specials, and
    # ``abandon_seasons`` would otherwise unmonitor every special when
    # all queue rows happen to be specials. Specials are typically opt-in
    # monitored separately — we never want to auto-unmonitor them.
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
            "searching_for_seconds": searching_for_seconds,
        },
    )
    abandon_seasons(
        conn,
        secret_key,
        series_id=arr_id,
        season_numbers=seasons,
        dl_id=dl_id,
    )
