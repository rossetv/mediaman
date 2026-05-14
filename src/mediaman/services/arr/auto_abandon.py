"""Auto-abandon escalation policy for long-stalled monitored items.

When a monitored Radarr/Sonarr item has been sitting in `searching` state
beyond ``_AUTO_ABANDON_AFTER_SECONDS`` (14 days), and the operator has
enabled the ``auto_abandon_enabled`` setting, this module unmonitors it
and emits a ``sec:auto_abandon.fired`` audit row. The audit row makes a
compromised-settings attack discoverable after the fact.

The policy logic and its audit guarantees are isolated here, separate
from the trigger-decision state machine in
:mod:`mediaman.services.arr.search_trigger` (which calls
:func:`maybe_auto_abandon` per item).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from mediaman.core.audit import security_event
from mediaman.services.infra import get_bool_setting

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


@dataclass(frozen=True, slots=True)
class _AbandonDecision:
    """The parsed, guard-cleared inputs an abandon branch needs.

    Produced by :func:`_should_auto_abandon` only when every guard has
    passed; carries the values the movie/series branches would otherwise
    re-parse from the raw ``item`` mapping.
    """

    dl_id: str
    arr_id: int
    searching_for_seconds: int


def _should_auto_abandon(
    conn: sqlite3.Connection, item: Mapping[str, object], now: float
) -> _AbandonDecision | None:
    """Run the auto-abandon guard cascade; return parsed inputs or ``None``.

    Returns ``None`` (skip — do not abandon) when any guard fails: the
    setting is off, the item is upcoming, its release date is unknown or
    too recent, its ``added_at`` is missing, it hasn't been searching long
    enough, or ``dl_id``/``arr_id`` are missing. Otherwise returns the
    parsed :class:`_AbandonDecision`. The logic here is lifted verbatim
    from the original guard preamble of :func:`maybe_auto_abandon`.
    """
    if not get_bool_setting(conn, "auto_abandon_enabled", default=False):
        return None
    if item.get("is_upcoming"):
        # Movies/series the user sees under "Coming soon" — Radarr/Sonarr
        # correctly won't find indexer matches yet, and abandoning them
        # would silently unmonitor something the user is actively waiting
        # for.
        return None
    _released_at_raw = item.get("released_at")
    released_at: float = (
        float(_released_at_raw) if isinstance(_released_at_raw, (int, float)) else 0.0
    )
    if released_at <= 0.0:
        # Release date unknown — be conservative and never abandon. The
        # alternative (assuming "released long ago") would mistakenly
        # unmonitor coming-soon entries whose dates haven't been filled
        # in by metadata providers yet.
        return None
    if now - released_at < _AUTO_ABANDON_RELEASE_GRACE_SECONDS:
        # Too fresh: NZBs/torrents may still be propagating. Auto-abandon
        # is meant to clean up genuinely stuck items, not bin recent
        # releases that just haven't surfaced yet.
        return None
    _added_at_raw = item.get("added_at")
    added_at: float = float(_added_at_raw) if isinstance(_added_at_raw, (int, float)) else 0.0
    if added_at <= 0.0:
        # No reliable timestamp — treat as unknown age and skip rather than
        # immediately abandoning (now - 0.0 ≈ 1.7e9 s, way past any threshold).
        return None
    if now - added_at < _AUTO_ABANDON_AFTER_SECONDS:
        return None

    _dl_id_raw = item.get("dl_id")
    dl_id: str = str(_dl_id_raw) if isinstance(_dl_id_raw, str) else ""
    _arr_id_raw = item.get("arr_id")
    arr_id: int = int(_arr_id_raw) if isinstance(_arr_id_raw, int) else 0
    if not dl_id or not arr_id:
        return None

    return _AbandonDecision(dl_id=dl_id, arr_id=arr_id, searching_for_seconds=int(now - added_at))


def _abandon_movie_with_audit(
    conn: sqlite3.Connection, secret_key: str, decision: _AbandonDecision
) -> None:
    """Emit the audit row, then abandon the movie.

    SECURITY: ``security_event`` is called *strictly before* the
    destructive ``abandon_movie`` so the trail records the policy firing
    even if Radarr is down — see the module docstring.
    """
    # Late import breaks the otherwise-circular dependency between
    # auto_abandon and the abandon service (which itself imports
    # clear_throttle from the throttle module).
    from mediaman.services.downloads.abandon import abandon_movie

    # Audit BEFORE the abandon call so the trail records the policy
    # firing even if Radarr is down. Pass actor="" so the row is
    # marked as a system-driven (not admin-triggered) event.
    security_event(
        conn,
        event="auto_abandon.fired",
        actor="",
        ip="",
        detail={
            "dl_id": decision.dl_id,
            "arr_id": decision.arr_id,
            "service": "radarr",
            "kind": "movie",
            "searching_for_seconds": decision.searching_for_seconds,
        },
    )
    abandon_movie(conn, secret_key, arr_id=decision.arr_id, dl_id=decision.dl_id)


def _abandon_series_with_audit(
    conn: sqlite3.Connection,
    secret_key: str,
    decision: _AbandonDecision,
    item: Mapping[str, object],
) -> None:
    """Emit the audit row, then abandon the series' positive-numbered seasons.

    Series with no positive-numbered seasons in the queue are skipped
    (Sonarr only knows about specials there). SECURITY: ``security_event``
    is called *strictly before* the destructive ``abandon_seasons`` —
    see the module docstring.
    """
    # Late import breaks the otherwise-circular dependency between
    # auto_abandon and the abandon service (which itself imports
    # clear_throttle from the throttle module).
    from mediaman.services.downloads.abandon import abandon_seasons

    # Filter season 0 (specials): Sonarr uses S00 for specials, and
    # ``abandon_seasons`` would otherwise unmonitor every special when
    # all queue rows happen to be specials. Specials are typically opt-in
    # monitored separately — we never want to auto-unmonitor them.
    _episodes_raw = item.get("episodes")
    _episodes: list[object] = list(_episodes_raw) if isinstance(_episodes_raw, list) else []
    seasons = sorted(
        {
            int(ep.get("season_number") or 0)
            for ep in _episodes
            if isinstance(ep, dict) and int(ep.get("season_number") or 0) > 0
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
            "dl_id": decision.dl_id,
            "arr_id": decision.arr_id,
            "service": "sonarr",
            "kind": "series",
            "seasons": seasons,
            "searching_for_seconds": decision.searching_for_seconds,
        },
    )
    abandon_seasons(
        conn,
        secret_key,
        series_id=decision.arr_id,
        season_numbers=seasons,
        dl_id=decision.dl_id,
    )


def maybe_auto_abandon(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    item: Mapping[str, object],
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
    identical. The guard cascade lives in :func:`_should_auto_abandon`;
    each branch emits its ``security_event`` audit row *before* the
    destructive abandon call.
    """
    decision = _should_auto_abandon(conn, item, now)
    if decision is None:
        return

    if item.get("kind") == "movie":
        _abandon_movie_with_audit(conn, secret_key, decision)
        return
    _abandon_series_with_audit(conn, secret_key, decision, item)
