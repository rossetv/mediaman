"""Downloads Radarr or Sonarr will not import, removed and searched for again unasked.

A film's download, or a Sonarr season's, that has needed attention for an hour is removed
from the download client with its release blocklisted, and the same film or episodes are
searched for again. Each film or season is given ``MAX_TRIES`` copies so; one that will not
import after that is left as it is, for an admin. While another copy is looked for, its card
says so. Library files and Plex are never touched, nor a download still coming in or one
importing as it should.

``note_stuck_imports`` runs on every fetch of a queue and keeps the clock; ``retry_stuck_imports``
runs from the hourly poller and acts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.arr._transport import ArrError
from mediaman.services.arr.fetcher._base import ArrCard, make_arr_card
from mediaman.services.downloads.download_format import (
    format_episode_label,
    map_arr_status,
    map_queue_state,
)
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError, get_bool_setting

logger = logging.getLogger(__name__)

SETTING = "import_retry_enabled"
MAX_TRIES = 2
AUDIT_ACTION = "import_retried"

_STUCK_FOR = timedelta(hours=1)
# The poller runs hourly, so one first seen stuck on a run has been stuck an hour on the next
# give or take how long a run takes.
_POLL_SLACK = timedelta(minutes=2)
# Another copy not grabbed in this long is no longer waited for.
_FINDING_FOR = timedelta(hours=24)

_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ConfigDecryptError, ValueError)


@dataclass(frozen=True, slots=True)
class _Unit:
    """A film's queue record, or the records of one season of a show, tried as one."""

    key: str
    stuck: bool
    # The records that need attention, which alone are removed.
    held_ids: frozenset[int]
    queue_ids: frozenset[int]
    label: str
    reason: str
    search: dict[str, Any]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _held(entry: Mapping[str, object]) -> bool:
    return (
        map_arr_status(
            str(entry.get("status") or ""),
            str(entry.get("tracked_state") or ""),
            str(entry.get("tracked_status") or ""),
        )
        == "needs_attention"
    )


def _units(card: Mapping[str, object]) -> list[_Unit]:
    if card.get("kind") == "movie":
        movie_id, queue_id = _int(card.get("movie_id")), _int(card.get("queue_id"))
        if not movie_id or not queue_id:
            return []
        stuck = _held(card)
        return [
            _Unit(
                key=f"radarr:{movie_id}",
                stuck=stuck,
                held_ids=frozenset({queue_id}) if stuck else frozenset(),
                queue_ids=frozenset({queue_id}),
                label="",
                reason=str(card.get("attention") or ""),
                search={"name": "MoviesSearch", "movieIds": [movie_id]},
            )
        ]
    series_id = _int(card.get("series_id"))
    episodes = card.get("episodes")
    if not series_id or not isinstance(episodes, list):
        return []
    seasons: dict[int, list[Mapping[str, object]]] = {}
    for ep in episodes:
        if isinstance(ep, Mapping) and _int(ep.get("queue_id")):
            seasons.setdefault(_int(ep.get("season_number")), []).append(ep)
    units = []
    for season, entries in sorted(seasons.items()):
        held = [e for e in entries if _held(e)]
        downloads = {str(e.get("download_id") or "") for e in held}
        search: dict[str, Any]
        if len(held) > 1 and len(downloads) == 1 and "" not in downloads:
            # A season pack, as Sonarr searches again for one itself.
            search = {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season}
        else:
            ids = sorted({_int(e.get("episode_id")) for e in held} - {0})
            search = {"name": "EpisodeSearch", "episodeIds": ids}
        units.append(
            _Unit(
                key=f"sonarr:{series_id}:{season}",
                stuck=map_queue_state(entries) == "needs_attention" and bool(held),
                held_ids=frozenset(_int(e.get("queue_id")) for e in held),
                queue_ids=frozenset(_int(e.get("queue_id")) for e in entries),
                label=format_episode_label(season, None),
                reason=next((str(e.get("attention")) for e in held if e.get("attention")), ""),
                search=search,
            )
        )
    return units


def _ids(text: object) -> set[int]:
    try:
        found = json.loads(text) if isinstance(text, str) else []
    except ValueError:
        return set()
    return {_int(i) for i in found} if isinstance(found, list) else set()


def _key_id(key: str) -> int:
    """The Radarr film's or Sonarr series' id in *key*."""
    number = key.split(":")[1]
    return int(number) if number.isdecimal() else 0


def _shown(text: object) -> dict[str, Any]:
    try:
        found = json.loads(text) if isinstance(text, str) else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _show_finding(service: str, key: str, shown: Mapping[str, Any], cards: list[ArrCard]) -> None:
    """Have the card of *key*'s title say another copy is looked for, adding one if the fetch
    gave none: a show with episodes in the library is not listed while it has none queued."""
    arr_id = _key_id(key)
    kind = "movie" if service == "radarr" else "series"
    for card in cards:
        if card.get("kind") != kind:
            continue
        if card.get("arr_id") == arr_id or card.get("dl_id") == shown.get("dl_id"):
            card["finding_copy"] = True
            return
        if card.get("series_id" if kind == "series" else "movie_id") == arr_id:
            # Its other downloads say what it is doing.
            return
    title = shown.get("title")
    if not isinstance(title, str) or not title:
        return
    year = shown.get("year")
    card = make_arr_card(
        kind,
        title,
        source=service.capitalize(),
        year=year if isinstance(year, int) else None,
        poster_url=str(shown.get("poster_url") or ""),
    )
    if isinstance(dl_id := shown.get("dl_id"), str) and dl_id:
        card["dl_id"] = dl_id
    if tmdb_id := _int(shown.get("tmdb_id")):
        card["tmdb_id"] = tmdb_id
    card["finding_copy"] = True
    cards.append(card)


def note_stuck_imports(
    conn: sqlite3.Connection, service: str, cards: list[ArrCard], *, complete: bool
) -> None:
    """Bring the clock of each of *service*'s downloads that will not import up to date, and
    mark, or add, the cards of the titles another copy of which is looked for.

    *complete* is False when the service did not give its whole list; nothing is then
    forgotten for being missing from it.
    """
    now_dt = now_utc()
    now = now_dt.isoformat()
    try:
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT key, stuck_since, retried_at, removed, shown FROM arr_import_retries"
                " WHERE key LIKE ?",
                (f"{service}:%",),
            )
        }
        writes: list[tuple[str, tuple[object, ...]]] = []
        seen: set[str] = set()
        for card in cards:
            for unit in _units(card):
                row = rows.get(unit.key)
                if row is not None and row[2] and unit.queue_ids <= _ids(row[3]):
                    # The records just removed, which the queue may list a moment longer.
                    continue
                seen.add(unit.key)
                if unit.stuck and row is None:
                    writes.append(
                        (
                            "INSERT INTO arr_import_retries (key, stuck_since) VALUES (?, ?)",
                            (unit.key, now),
                        )
                    )
                elif row is not None and (
                    (unit.stuck and row[1] is None) or (not unit.stuck and (row[1] or row[2]))
                ):
                    writes.append(
                        (
                            "UPDATE arr_import_retries SET stuck_since=?, retried_at=NULL,"
                            " removed=NULL, search=NULL, shown=NULL WHERE key=?",
                            (now if unit.stuck else None, unit.key),
                        )
                    )
        for key, row in rows.items():
            if key in seen:
                continue
            started = parse_iso_utc(row[2]) if row[2] else None
            if started is None:
                if complete:
                    # It left the queue: imported, or removed by someone.
                    writes.append(("DELETE FROM arr_import_retries WHERE key=?", (key,)))
                continue
            gone = complete and service == "radarr" and not _listed(key, cards)
            if gone or not timedelta() <= now_dt - started < _FINDING_FOR:
                writes.append(("DELETE FROM arr_import_retries WHERE key=?", (key,)))
                continue
            _show_finding(service, key, _shown(row[4]), cards)
        if writes:
            with conn:
                for sql, params in writes:
                    conn.execute(sql, params)
    except sqlite3.DatabaseError:
        logger.warning(
            "Failed to record which %s downloads will not import", service, exc_info=True
        )


def _listed(key: str, cards: Sequence[ArrCard]) -> bool:
    """Whether Radarr still lists the film of *key* as wanted: one with its file has no card."""
    movie_id = _key_id(key)
    return any(card.get("kind") == "movie" and card.get("arr_id") == movie_id for card in cards)


def _client(conn: sqlite3.Connection, secret_key: str, service: str) -> Any:
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    build = build_radarr_from_db if service == "radarr" else build_sonarr_from_db
    return build(conn, secret_key)


def _history_line(shown: Mapping[str, Any], tries: int) -> str:
    name = f"'{shown.get('title') or 'A download'}'"
    if shown.get("label"):
        name += f" {shown['label']}"
    reason = f" ({shown['reason']})" if shown.get("reason") else ""
    return (
        f"{name} couldn't be imported{reason}; removed and searching for another copy"
        f" (try {tries} of {MAX_TRIES})"
    )


def _search(conn: sqlite3.Connection, client: Any, key: str, search: str, shown: str) -> None:
    """Send the search of a removed download, and count the try once it is sent."""
    try:
        client.send_command(json.loads(search))
    except _ERRORS:
        logger.warning(
            "Could not search again for %s; trying again on the next run", key, exc_info=True
        )
        return
    with conn:
        tries_row = conn.execute(
            "UPDATE arr_import_retries SET tries = tries + 1, search = NULL"
            " WHERE key=? AND search IS NOT NULL RETURNING tries",
            (key,),
        ).fetchone()
        if tries_row is not None:
            log_audit(conn, key, AUDIT_ACTION, _history_line(_shown(shown), tries_row[0]))
    logger.info("Removed %s's download that would not import and searched again", key)


def _retry(
    conn: sqlite3.Connection,
    secret_key: str,
    card: Mapping[str, object],
    unit: _Unit,
    now: datetime,
) -> None:
    row = conn.execute(
        "SELECT stuck_since, tries FROM arr_import_retries WHERE key=? AND retried_at IS NULL",
        (unit.key,),
    ).fetchone()
    since = parse_iso_utc(row[0]) if row is not None and row[0] else None
    if since is None or row[1] >= MAX_TRIES or now - since < _STUCK_FOR - _POLL_SLACK:
        return
    service = unit.key.partition(":")[0]
    shown = json.dumps(
        {
            "dl_id": card.get("dl_id"),
            "title": card.get("title"),
            "year": card.get("year"),
            "poster_url": card.get("poster_url"),
            "tmdb_id": card.get("tmdb_id"),
            "label": unit.label,
            "reason": unit.reason,
        }
    )
    search = json.dumps(unit.search)
    with conn:
        # Taken by the one run that changes the row, should two ever run at once.
        claimed = conn.execute(
            "UPDATE arr_import_retries SET stuck_since=NULL, retried_at=?, removed=?, search=?,"
            " shown=? WHERE key=? AND stuck_since=? AND retried_at IS NULL AND tries=?",
            (
                now.isoformat(),
                json.dumps(sorted(unit.held_ids)),
                search,
                shown,
                unit.key,
                row[0],
                row[1],
            ),
        ).rowcount
    if claimed != 1:
        return
    try:
        client = _client(conn, secret_key, service)
        if client is None:
            raise ArrError(f"{service} is not set up")
        for queue_id in sorted(unit.held_ids):
            client.remove_from_queue(queue_id)
    except _ERRORS:
        logger.warning(
            "Could not remove %s's download that would not import; trying again on the next run",
            unit.key,
            exc_info=True,
        )
        with conn:
            conn.execute(
                "UPDATE arr_import_retries SET stuck_since=?, retried_at=NULL, removed=NULL,"
                " search=NULL, shown=NULL WHERE key=?",
                (row[0], unit.key),
            )
        return
    _search(conn, client, unit.key, search, shown)


def retry_stuck_imports(
    conn: sqlite3.Connection, secret_key: str, cards: Sequence[Mapping[str, object]]
) -> None:
    """Remove and search again for each download of *cards* that has not imported for an hour,
    and send each search a run before could not; nothing while the setting is off."""
    if not get_bool_setting(conn, SETTING, default=True):
        return
    for key, search, shown in conn.execute(
        "SELECT key, search, shown FROM arr_import_retries"
        " WHERE retried_at IS NOT NULL AND search IS NOT NULL"
    ).fetchall():
        try:
            client = _client(conn, secret_key, key.partition(":")[0])
        except _ERRORS:
            client = None
        if client is not None:
            _search(conn, client, key, search, shown)
    now = now_utc()
    for card in cards:
        for unit in _units(card):
            if unit.stuck:
                _retry(conn, secret_key, card, unit, now)
