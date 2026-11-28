"""Downloads Radarr or Sonarr will not import, mended or replaced unasked.

A film's download, or a Sonarr season's, that has been stuck for an hour is first mended where
it lies when it can be: its video files given the extension they lack, or the files the service
matched by id alone imported by hand (see ``import_fix``). Otherwise, or when it is still stuck
an hour after that, it is removed from the download client with its release blocklisted, and the
same film or episodes are searched for again; one the service will not import as it is no
better than the file the title has is removed so, and nothing is searched for. A stalled
download is replaced the same way once it has gone nowhere for a while. A download is mended in place once for each copy, so one that
comes and goes is not mended over and over. Each next copy is left longer before it is replaced,
by ``stuck_modes.wait_for``, and then once a day, for as long as the title does not import,
until ``SAME_REASON_TRIES`` copies in a row were replaced for the same reason: then mediaman stops,
says so, and waits for an admin to have it try again. No more than ``_REPLACED_PER_RUN`` downloads
of a service are replaced in a run, those stuck longest first. A download Radarr or Sonarr says is
coming in that has brought nothing for ``UNMOVING_FOR`` is taken as stalled, as neither says so of
a Usenet download. One held by the service's own setup rather than by its release is left for an admin; one
the service holds for a while and imports by itself, one paused, and one whose download client the
service cannot reach are waited for. While another copy is looked for,
its card says so. Library files and Plex are never touched, nor a download still coming in or
one importing as it should. Where the service searches again by itself on a removal, as older
ones do, mediaman sends no search of its own.

``note_stuck_imports`` runs on every fetch of a queue and keeps the clock; ``retry_stuck_imports``
runs from the hourly poller and acts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.arr import import_fix, stuck_modes
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
AUDIT_ACTION = "import_retried"
RENAMED_ACTION = "import_renamed"
IMPORTED_ACTION = "import_imported"
STOPPED_ACTION = "import_stopped"
NOT_UPGRADE_ACTION = "import_not_upgrade"

# How many copies in a row are replaced for the same reason before mediaman stops: one that is
# "Not an upgrade" would otherwise be downloaded again every day.
SAME_REASON_TRIES = 3
# How long a download said to be coming in may bring nothing before it is taken as stalled.
UNMOVING_FOR = timedelta(hours=3)

_STUCK_FOR = timedelta(hours=1)
# The poller runs hourly, so one first seen stuck on a run has been stuck an hour on the next
# give or take how long a run takes.
_POLL_SLACK = timedelta(minutes=2)
# Another copy not grabbed in this long is no longer waited for.
_FINDING_FOR = timedelta(hours=24)
# A title out of the queue, last tried this long ago, is forgotten with its tries.
_KEPT_FOR = timedelta(days=30)
# A title that has gone this long without importing is told of as stuck, though still tried.
LONG_STUCK = timedelta(days=7)
# The most downloads of one service replaced in a run, so many stuck at once are not all
# blocklisted and searched for at the same moment.
_REPLACED_PER_RUN = 2
# What mediaman will next do about a stuck title, as its card says.
PLAN_RENAME = "rename"
PLAN_IMPORT = "import"
PLAN_REPLACE = "replace"

_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ConfigDecryptError, ValueError)

# rationale: what is left of each download coming in, and since when, to tell one that brings
# nothing; kept in the process, as a restart only puts off telling it. Guarded by _moving_lock.
_moving: dict[str, tuple[int, datetime]] = {}
_moving_lock = threading.Lock()


def forget_progress() -> None:
    with _moving_lock:
        _moving.clear()


def _queue_entries(cards: Sequence[ArrCard]) -> list[Any]:
    """The queue records of *cards*: a film's card, or a show's episodes."""
    entries: list[Any] = []
    for card in cards:
        if card.get("kind") == "series":
            entries += card.get("episodes") or []
        elif card.get("queue_id"):
            entries.append(card)
    return entries


def _mark_unmoving(service: str, cards: Sequence[ArrCard], now: datetime, complete: bool) -> None:
    """Have each of *service*'s downloads that the service says is coming in, but that has
    brought nothing for ``UNMOVING_FOR``, say it stalled, as the service says of one itself."""
    seen: set[str] = set()
    with _moving_lock:
        for entry in _queue_entries(cards):
            key = f"{service}:{entry.get('download_id') or entry.get('queue_id')}"
            left = entry.get("sizeleft")
            coming = (
                str(entry.get("status") or "").lower() == "downloading"
                and str(entry.get("tracked_status") or "").lower() not in ("warning", "error")
                and isinstance(left, int)
                and left > 0
            )
            if not coming:
                _moving.pop(key, None)
                continue
            seen.add(key)
            was = _moving.get(key)
            if was is None or was[0] != left:
                _moving[key] = (left, now)
                continue
            if (still := now - was[1]) >= UNMOVING_FOR:
                hours = int(still.total_seconds() // 3600)
                entry["tracked_state"] = "downloading"
                entry["tracked_status"] = "warning"
                entry["attention"] = f"Stalled: no progress for {hours} hours"
        if complete:
            for key in [k for k in _moving if k.startswith(f"{service}:") and k not in seen]:
                del _moving[key]


@dataclass(frozen=True, slots=True)
class _Unit:
    """A film's queue record, or the records of one season of a show, tried as one."""

    key: str
    stuck: bool
    # The records that need attention, which alone are removed.
    held_ids: frozenset[int]
    # The download of each record that needs attention, by queue id.
    downloads: Mapping[int, str]
    # The episodes of the records that need attention; none of a film's.
    episodes: frozenset[int]
    queue_ids: frozenset[int]
    label: str
    reason: str
    search: dict[str, Any]
    # What keeps the records that need attention, as ``stuck_modes`` names it.
    mode: str = stuck_modes.OTHER
    # Of each download of the records that need attention, by its client id: the folder it is in
    # as the service sees it and its release's name, and the episodes of its records.
    places: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    download_episodes: Mapping[str, frozenset[int]] = field(default_factory=dict)
    # Whether the film, or every episode of the records that need attention, has a file already.
    has_file: bool = False


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mode(entry: Mapping[str, object]) -> str:
    return stuck_modes.classify(
        str(entry.get("status") or ""),
        str(entry.get("tracked_state") or ""),
        str(entry.get("tracked_status") or ""),
        str(entry.get("attention") or ""),
    )


def _held(entry: Mapping[str, object]) -> bool:
    """Whether *entry* needs attention, or is a download that stalled."""
    state = map_arr_status(
        str(entry.get("status") or ""),
        str(entry.get("tracked_state") or ""),
        str(entry.get("tracked_status") or ""),
    )
    return state == "needs_attention" or _mode(entry) == stuck_modes.STALLED


def _unit_mode(modes: set[str]) -> str:
    """The mode of records held for *modes*: one that keeps any from being acted on keeps all."""
    if waiting := sorted(modes - stuck_modes.ACTIONABLE):
        return waiting[0]
    return modes.pop() if len(modes) == 1 else stuck_modes.OTHER


def _unit(
    key: str, entries: Sequence[Mapping[str, object]], label: str, search: dict[str, Any]
) -> _Unit:
    held = [e for e in entries if _held(e)]
    reasons = [str(e.get("attention") or "") for e in held]
    modes = {_mode(e) for e in held}
    # One still coming in as it should holds the rest back, as another copy may be on its way.
    coming = any(not _held(e) and map_queue_state([e]) == "downloading" for e in entries)
    places: dict[str, tuple[str, str]] = {}
    episodes: dict[str, set[int]] = {}
    for e in held:
        if download := str(e.get("download_id") or ""):
            places.setdefault(
                download, (str(e.get("output_path") or ""), str(e.get("release") or ""))
            )
            episodes.setdefault(download, set()).add(_int(e.get("episode_id")))
    return _Unit(
        key=key,
        stuck=bool(held) and not coming,
        held_ids=frozenset(_int(e.get("queue_id")) for e in held),
        downloads={_int(e.get("queue_id")): str(e.get("download_id") or "") for e in held},
        episodes=frozenset(_int(e.get("episode_id")) for e in held) - {0},
        queue_ids=frozenset(_int(e.get("queue_id")) for e in entries),
        label=label,
        reason=next((r for r in reasons if r), ""),
        search=search,
        mode=_unit_mode(modes),
        places=places,
        download_episodes={d: frozenset(ids - {0}) for d, ids in episodes.items()},
        has_file=bool(held) and all(e.get("has_file") is True for e in held),
    )


def _season_units(card: Mapping[str, object]) -> list[_Unit]:
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
            _unit(
                f"sonarr:{series_id}:{season}", entries, format_episode_label(season, None), search
            )
        )
    return units


def _release_of(card: Mapping[str, object]) -> str:
    names = card.get("release_names")
    return str(names[0]) if isinstance(names, list) and names else ""


def _units(cards: Sequence[Mapping[str, object]]) -> list[tuple[Mapping[str, object], _Unit]]:
    """Each film's or season's unit of *cards*, with a card of its title; Radarr gives a card
    for each queue record, so a film's are one unit."""
    films: dict[int, list[Mapping[str, object]]] = {}
    units: list[tuple[Mapping[str, object], _Unit]] = []
    for card in cards:
        if card.get("kind") != "movie":
            units.extend((card, unit) for unit in _season_units(card))
        elif (movie_id := _int(card.get("movie_id"))) and _int(card.get("queue_id")):
            films.setdefault(movie_id, []).append(card)
    for movie_id, records in films.items():
        search = {"name": "MoviesSearch", "movieIds": [movie_id]}
        named = [{**r, "release": _release_of(r)} for r in records]
        units.append((records[0], _unit(f"radarr:{movie_id}", named, "", search)))
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
    season = key.rpartition(":")[2]
    for card in cards:
        if card.get("kind") != kind:
            continue
        queued = card.get("series_id" if kind == "series" else "movie_id") == arr_id
        episodes = card.get("episodes")
        if (
            queued
            and kind == "series"
            and isinstance(episodes, list)
            and any(str(_int(ep.get("season_number"))) != season for ep in episodes)
        ):
            # Its other seasons' downloads say what it is doing.
            return
        if queued or card.get("arr_id") == arr_id or card.get("dl_id") == shown.get("dl_id"):
            card["finding_copy"] = True
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


class _Planner:
    """What mediaman will next do about each stuck unit, reading the downloads folder once."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._root: Path | bool | None = False

    def _folder(self) -> Path | None:
        if self._root is False:
            self._root = import_fix.downloads_folder(self._conn)
        return self._root if isinstance(self._root, Path) else None

    def renamable(self, unit: _Unit) -> bool:
        """Whether a folder of *unit*'s downloads lies where mediaman may rename in it."""
        root = self._folder()
        return root is not None and any(
            import_fix.release_folder(root, output_path, release)
            for output_path, release in unit.places.values()
        )

    def plan(self, unit: _Unit, *, may_fix: bool) -> str:
        """Its next step: mended in place while *may_fix* and its mode allows, else replaced; ""
        when it is left as it is."""
        if unit.mode not in stuck_modes.ACTIONABLE:
            return ""
        if may_fix and unit.mode == stuck_modes.NO_FILES and self.renamable(unit):
            return PLAN_RENAME
        if may_fix and unit.mode == stuck_modes.WRONG_MATCH and unit.places:
            return PLAN_IMPORT
        return PLAN_REPLACE


def _next_try(
    plan: str, mode: str, since: datetime, tries: int, fixed: datetime | None
) -> str | None:
    """When the poller next acts on a unit stuck *since*, by its *plan*, after *tries* copies,
    whose copy was last mended in place at *fixed*; None when it is left as it is."""
    if not plan:
        return None
    due = since + _STUCK_FOR
    if plan == PLAN_REPLACE:
        due = max(due, since + stuck_modes.wait_for(tries, mode))
        if fixed is not None:
            due = max(due, fixed + _STUCK_FOR)
    return due.isoformat()


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
    _mark_unmoving(service, cards, now_dt, complete)
    try:
        enabled = get_bool_setting(conn, SETTING, default=True)
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT key, stuck_since, retried_at, removed, shown, tries, search, first_stuck_at,"
                " fix_plan, fixed_tries, fixed_at, next_try_at FROM arr_import_retries"
                " WHERE key LIKE ?",
                (f"{service}:%",),
            )
        }
        writes: list[tuple[str, tuple[object, ...]]] = []
        seen: set[str] = set()
        reset = (
            "UPDATE arr_import_retries SET stuck_since=?, retried_at=NULL, removed=NULL,"
            " search=NULL, shown=NULL WHERE key=?"
        )
        planner = _Planner(conn)
        for _card, unit in _units(cards):
            row = rows.get(unit.key)
            if row is not None and row[2] and unit.queue_ids <= _ids(row[3]):
                # The records just removed, which the queue may list a moment longer.
                continue
            seen.add(unit.key)
            if row is not None and row[6] is not None:
                # A removal's search still to send goes, whatever was grabbed meanwhile.
                continue
            if unit.stuck and row is None:
                plan = planner.plan(unit, may_fix=True)
                writes.append(
                    (
                        "INSERT INTO arr_import_retries"
                        " (key, stuck_since, first_stuck_at, fix_plan, next_try_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (unit.key, now, now, plan, _next_try(plan, unit.mode, now_dt, 0, None)),
                    )
                )
                continue
            if row is not None and unit.stuck:
                plan = planner.plan(unit, may_fix=row[9] is None or row[9] != row[5])
                if plan != row[8]:
                    writes.append(
                        ("UPDATE arr_import_retries SET fix_plan=? WHERE key=?", (plan, unit.key))
                    )
                since = parse_iso_utc(row[1]) if row[1] else now_dt
                fixed = parse_iso_utc(row[10]) if row[10] and row[9] == row[5] else None
                next_try = _next_try(plan, unit.mode, since or now_dt, row[5], fixed)
                if next_try != row[11]:
                    writes.append(
                        (
                            "UPDATE arr_import_retries SET next_try_at=? WHERE key=?",
                            (next_try, unit.key),
                        )
                    )
            if row is not None and unit.stuck and row[7] is None:
                # Since when it has gone without importing, of one noted before that was kept.
                writes.append(
                    (
                        "UPDATE arr_import_retries SET first_stuck_at=? WHERE key=?",
                        (row[1] or now, unit.key),
                    )
                )
            if row is not None and unit.stuck and row[1] is None:
                writes.append((reset, (now, unit.key)))
            elif row is not None and not unit.stuck and (row[1] or row[2]):
                writes.append((reset, (None, unit.key)))
        for key, row in rows.items():
            if key in seen:
                continue
            started = parse_iso_utc(row[2]) if row[2] else None
            if started is None:
                # It left the queue. Tries spent are kept until the title has its files, which
                # the poller asks; else there is nothing to keep.
                if complete and not row[5]:
                    writes.append(("DELETE FROM arr_import_retries WHERE key=?", (key,)))
                elif complete and row[1]:
                    writes.append((reset, (None, key)))
                continue
            if row[6] is None:
                gone = complete and service == "radarr" and not _listed(key, cards)
                if not enabled or gone or not timedelta() <= now_dt - started < _FINDING_FOR:
                    writes.append((reset, (None, key)))
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
    """Whether Radarr still lists the film of *key*, queued or wanted: one with its file has no
    card."""
    movie_id = _key_id(key)
    return any(
        card.get("kind") == "movie" and movie_id in (card.get("arr_id"), card.get("movie_id"))
        for card in cards
    )


def _client(conn: sqlite3.Connection, secret_key: str, service: str) -> Any:
    from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db

    build = build_radarr_from_db if service == "radarr" else build_sonarr_from_db
    return build(conn, secret_key)


def _name(shown: Mapping[str, Any]) -> str:
    name = f"'{shown.get('title') or 'A download'}'"
    return f"{name} {shown['label']}" if shown.get("label") else name


def _history_line(shown: Mapping[str, Any], tries: int) -> str:
    reason = f" ({shown['reason']})" if shown.get("reason") else ""
    line = (
        f"{_name(shown)} couldn't be imported{reason}; blocklisted that copy and searching for"
        f" another (try {tries})"
    )
    days = shown.get("stuck_days")
    if isinstance(days, int) and days >= LONG_STUCK.days:
        line += f". It has not imported for {days} days; mediaman keeps trying once a day"
    return line


def _shown_of(card: Mapping[str, object], unit: _Unit, **more: object) -> dict[str, Any]:
    """What History and the card say of *unit*'s title while it is mended or replaced."""
    return {
        "dl_id": card.get("dl_id"),
        "title": card.get("title"),
        "year": card.get("year"),
        "poster_url": card.get("poster_url"),
        "tmdb_id": card.get("tmdb_id"),
        "label": unit.label,
        "reason": unit.reason,
        **more,
    }


def _no_answer(exc: Exception) -> bool:
    """Whether *exc* says the service gave no answer, or one its setup alone explains: it timed
    out or could not be reached, or it would not take mediaman's key."""
    if isinstance(exc, SafeHTTPError):
        return exc.status_code in (0, 401, 403)
    return isinstance(exc, requests.Timeout | requests.ConnectionError | ConfigDecryptError)


def _refused(exc: Exception) -> bool:
    return isinstance(exc, SafeHTTPError) and 400 <= exc.status_code < 500 and not _no_answer(exc)


def _search(
    conn: sqlite3.Connection,
    client: Any,
    key: str,
    search: str,
    shown: str,
    since: datetime | None,
    removed: bool = False,
) -> bool:
    """Send the search of a removed download, unless every record of it was *removed* just now
    by a service that then searches by itself, and write its try to History once it is sent;
    one the service has refused since a removal *since* ``_FINDING_FOR`` ago is given up.
    Whether the service gave no answer."""
    try:
        if not (removed and client.searches_when_removed()):
            client.send_command(json.loads(search))
    except _ERRORS as exc:
        refused = _refused(exc)
        if refused and since is not None and now_utc() - since >= _FINDING_FOR:
            logger.warning(
                "%s's search for another copy is refused; no longer sending it",
                key,
                exc_info=True,
            )
            with conn:
                conn.execute(
                    "UPDATE arr_import_retries SET retried_at=NULL, removed=NULL, search=NULL,"
                    " shown=NULL WHERE key=? AND search IS NOT NULL",
                    (key,),
                )
            return False
        logger.warning(
            "Could not search again for %s; trying again on the next run", key, exc_info=True
        )
        return _no_answer(exc)
    with conn:
        tries_row = conn.execute(
            "UPDATE arr_import_retries SET search = NULL"
            " WHERE key=? AND search IS NOT NULL RETURNING tries",
            (key,),
        ).fetchone()
        if tries_row is not None:
            log_audit(conn, key, AUDIT_ACTION, _history_line(_shown(shown), tries_row[0]))
    logger.info("Removed %s's download that would not import and searched again", key)
    return False


def _mark_fixed(conn: sqlite3.Connection, key: str, stuck_since: str, now: datetime) -> bool:
    """Take *key*'s one mend in place for the copy it is on, as one run alone."""
    with conn:
        return (
            conn.execute(
                "UPDATE arr_import_retries SET fixed_at=?, fixed_tries=tries, fix_plan=? WHERE key=?"
                " AND stuck_since=? AND retried_at IS NULL"
                " AND (fixed_tries IS NULL OR fixed_tries != tries)",
                (now.isoformat(), PLAN_REPLACE, key, stuck_since),
            ).rowcount
            == 1
        )


def _unmark_fixed(conn: sqlite3.Connection, key: str, row: Any) -> None:
    """Give *key* back the mend a send that surely failed did not make."""
    with conn:
        conn.execute(
            "UPDATE arr_import_retries SET fixed_at=?, fixed_tries=? WHERE key=?",
            (row[2], row[4], key),
        )


def _rename_in_place(
    conn: sqlite3.Connection, client: Any, card: Mapping[str, object], unit: _Unit, row: Any
) -> bool:
    """Give the video files of each of *unit*'s downloads the extension they lack and have the
    service import the folders of those renamed again. Whether any file was renamed."""
    root = import_fix.downloads_folder(conn)
    if root is None:
        return False
    renamed: list[str] = []
    rescans: list[tuple[str, str]] = []
    for download, (output_path, release) in sorted(unit.places.items()):
        folder = import_fix.release_folder(root, output_path, release)
        if folder and (names := import_fix.rename_videos(folder)):
            renamed += names
            rescans.append((output_path, download))
    if not renamed or not _mark_fixed(conn, unit.key, row[0], now_utc()):
        return bool(renamed)
    service = unit.key.partition(":")[0]
    plural = "file" if len(renamed) == 1 else "files"
    title = _name(_shown_of(card, unit))
    logger.info(
        "import_retry.renamed title=%s files=%d mode=%s service=%s",
        title,
        len(renamed),
        unit.mode,
        service,
    )
    # Committed before the service is asked, so no write waits on its answer.
    with conn:
        log_audit(
            conn,
            unit.key,
            RENAMED_ACTION,
            f"{title} couldn't be imported ({unit.reason}); renamed"
            f" {len(renamed)} {plural} so {service.capitalize()} could import"
            f" {'it' if len(renamed) == 1 else 'them'}",
        )
    try:
        for output_path, download in rescans:
            client.send_command(import_fix.scan_command(service, output_path, download))
    except _ERRORS:
        # The service tries a pending import again by itself.
        logger.warning("Could not ask %s to import %s again", service, unit.key, exc_info=True)
    return True


def _import_by_hand(
    conn: sqlite3.Connection, client: Any, card: Mapping[str, object], unit: _Unit, row: Any
) -> bool:
    """Import the files of *unit*'s downloads by hand, of each download whose files the
    service's own reading names its title and episodes it waits for beyond doubt. Whether any
    were sent."""
    service = unit.key.partition(":")[0]
    arr_id = _key_id(unit.key)
    files: list[dict[str, Any]] = []
    for download in sorted(unit.places):
        listed = client._get_list(import_fix.manual_import_query(service, download, arr_id))
        episodes = unit.download_episodes.get(download, frozenset())
        files += import_fix.manual_import_files(service, listed, arr_id, episodes)
    if not files or not _mark_fixed(conn, unit.key, row[0], now_utc()):
        return False
    try:
        client.send_command({"name": "ManualImport", "files": files, "importMode": "auto"})
    except _ERRORS as exc:
        if _may_have_acted(exc):
            # It may be importing: nothing more is done to it before the next run.
            logger.warning("%s's import by hand got no answer", unit.key, exc_info=True)
            return True
        _unmark_fixed(conn, unit.key, row)
        raise
    plural = "file" if len(files) == 1 else "files"
    with conn:
        log_audit(
            conn,
            unit.key,
            IMPORTED_ACTION,
            f"{_name(_shown_of(card, unit))} couldn't be imported ({unit.reason}); imported its"
            f" {len(files)} {plural} by hand in {service.capitalize()}",
        )
    return True


def _fix_in_place(
    conn: sqlite3.Connection, secret_key: str, card: Mapping[str, object], unit: _Unit, row: Any
) -> bool | None:
    """Mend *unit*'s download where it lies, when its mode allows; True when it was, None when
    its service gave no answer, False when nothing was done. A mend the service refused, or an
    import by hand of files in doubt, is this copy's one mend, so the next step is what is
    planned and shown."""
    fixes = {stuck_modes.NO_FILES: _rename_in_place, stuck_modes.WRONG_MATCH: _import_by_hand}
    fix = fixes.get(unit.mode)
    if fix is None or not unit.places:
        return False
    try:
        client = _client(conn, secret_key, unit.key.partition(":")[0])
        if client is None:
            return False
        mended = fix(conn, client, card, unit, row)
    except _ERRORS as exc:
        if _no_answer(exc):
            logger.warning(
                "Could not mend %s where it lies (%s); trying again on the next run", unit.key, exc
            )
            return None
        logger.warning("Could not mend %s where it lies (%s); trying the next step", unit.key, exc)
        mended = None
    if not mended and (mended is None or fix is _import_by_hand):
        with conn:
            conn.execute(
                "UPDATE arr_import_retries SET fixed_tries=tries, fix_plan=? WHERE key=?"
                " AND stuck_since=? AND retried_at IS NULL",
                (PLAN_REPLACE, unit.key, row[0]),
            )
    return bool(mended)


# What came of a unit's turn in a run.
_SILENT = "silent"
_REPLACED = "replaced"


def _retry(
    conn: sqlite3.Connection,
    secret_key: str,
    card: Mapping[str, object],
    unit: _Unit,
    now: datetime,
    *,
    may_replace: bool = True,
) -> str:
    """Mend *unit*'s download where it lies, or else, while *may_replace*, remove its records and
    search again, once it has been stuck long enough; one of them in a run. ``_SILENT`` when its
    service gave no answer, ``_REPLACED`` when it was replaced, else ""."""
    row = conn.execute(
        "SELECT stuck_since, tries, fixed_at, first_stuck_at, fixed_tries, last_reason,"
        " reason_tries, stopped_at FROM arr_import_retries WHERE key=? AND retried_at IS NULL",
        (unit.key,),
    ).fetchone()
    since = parse_iso_utc(row[0]) if row is not None and row[0] else None
    if since is None or now - since < _STUCK_FOR - _POLL_SLACK or row[7] is not None:
        return ""
    if unit.mode == stuck_modes.NOT_UPGRADE and unit.has_file:
        return _skip_not_upgrade(conn, secret_key, card, unit, row[0])
    if row[4] is None or row[4] != row[1]:
        mended = _fix_in_place(conn, secret_key, card, unit, row)
        if mended is None or mended:
            return _SILENT if mended is None else ""
    fixed = parse_iso_utc(row[2]) if row[2] else None
    if fixed is not None and row[4] == row[1] and now - fixed < _STUCK_FOR - _POLL_SLACK:
        return ""
    if not may_replace or now - since < stuck_modes.wait_for(row[1], unit.mode) - _POLL_SLACK:
        return ""
    same = row[5] == unit.reason
    if same and row[6] >= SAME_REASON_TRIES:
        _stop(conn, card, unit, now, row[6])
        return ""
    reason_tries = row[6] + 1 if same else 1
    service = unit.key.partition(":")[0]
    first = parse_iso_utc(row[3]) if row[3] else None
    stuck_days = (now - first).days if first is not None else 0
    shown = json.dumps(_shown_of(card, unit, stuck_days=stuck_days))
    search = json.dumps(unit.search)
    with conn:
        # Taken by the one run that changes the row, should two ever run at once; the try counts
        # from here, as the removal may go through whatever is heard of it.
        claimed = conn.execute(
            "UPDATE arr_import_retries SET stuck_since=NULL, tries=tries + 1, retried_at=?,"
            " removed=?, search=?, shown=?, tried_at=?, tried_episodes=?, last_reason=?,"
            " reason_tries=? WHERE key=? AND stuck_since=? AND retried_at IS NULL AND tries=?",
            (
                now.isoformat(),
                json.dumps(sorted(unit.held_ids)),
                search,
                shown,
                now.isoformat(),
                json.dumps(sorted(unit.episodes)),
                unit.reason,
                reason_tries,
                unit.key,
                row[0],
                row[1],
            ),
        ).rowcount
    if claimed != 1:
        return ""
    removed_any = False
    # A record already gone was failed by nobody here, so no search of the service's own
    # follows unless it went with a download this run removed.
    removed_ids: set[int] = set()
    removed_downloads: set[str] = set()
    client = None
    try:
        client = _client(conn, secret_key, service)
        if client is None:
            raise ArrError(f"{service} is not set up")
        for queue_id in sorted(unit.held_ids):
            download = unit.downloads.get(queue_id, "")
            if client.remove_from_queue(queue_id):
                removed_ids.add(queue_id)
                removed_downloads.add(download)
            removed_any = True
    except _ERRORS as exc:
        refused = _refused(exc)
        silent = client is None or _no_answer(exc)
        if removed_any or _may_have_acted(exc):
            # Removed with no search of its own: the search must still go, now or next run.
            logger.warning(
                "Could not remove all of %s's download that would not import",
                unit.key,
                exc_info=True,
            )
            _search(conn, client, unit.key, search, shown, now)
            return _SILENT if silent else _REPLACED
        # Refused outright, it is asked again after the next, longer wait.
        then = "trying again later" if refused else "trying again on the next run"
        logger.warning(
            "Could not remove %s's download that would not import; %s",
            unit.key,
            then,
            exc_info=True,
        )
        with conn:
            conn.execute(
                "UPDATE arr_import_retries SET stuck_since=?, tries=?, retried_at=NULL,"
                " removed=NULL, search=NULL, shown=NULL, last_reason=?, reason_tries=?"
                " WHERE key=?",
                (
                    now.isoformat() if refused else row[0],
                    row[1] + 1 if refused else row[1],
                    unit.reason if refused else row[5],
                    reason_tries if refused else row[6],
                    unit.key,
                ),
            )
        return _SILENT if silent else ""
    every_removed = all(
        queue_id in removed_ids or unit.downloads.get(queue_id, "") in removed_downloads - {""}
        for queue_id in unit.held_ids
    )
    silent = _search(conn, client, unit.key, search, shown, now, every_removed)
    return _SILENT if silent else _REPLACED


def _skip_not_upgrade(
    conn: sqlite3.Connection,
    secret_key: str,
    card: Mapping[str, object],
    unit: _Unit,
    stuck_since: str,
) -> str:
    """Remove *unit*'s download, which the service will not import as it is no better than the
    file the title has, with its release blocklisted, and search for nothing: the title has a
    copy. Said to History when a record was removed; ``_SILENT`` when the service gave no answer.
    """
    with conn:
        # Taken by the one run that changes the row; one still listed is stuck again on the next
        # fetch, and gone from the queue, it is forgotten as any other.
        claimed = conn.execute(
            "UPDATE arr_import_retries SET stuck_since=NULL WHERE key=? AND stuck_since=?"
            " AND retried_at IS NULL",
            (unit.key, stuck_since),
        ).rowcount
    if claimed != 1:
        return ""
    service = unit.key.partition(":")[0]
    removed = 0
    try:
        client = _client(conn, secret_key, service)
        if client is None:
            return _SILENT
        for queue_id in sorted(unit.held_ids):
            removed += bool(client.remove_from_queue(queue_id))
    except _ERRORS as exc:
        logger.warning(
            "Could not remove %s's download that is no upgrade; trying again later",
            unit.key,
            exc_info=True,
        )
        return _SILENT if _no_answer(exc) else ""
    logger.info("import_retry.not_upgrade key=%s removed=%d", unit.key, removed)
    if removed:
        with conn:
            log_audit(
                conn,
                unit.key,
                NOT_UPGRADE_ACTION,
                f"Skipped a download of {_name(_shown_of(card, unit))}: it wasn't better than"
                " the copy you have",
            )
    return ""


def _stop(
    conn: sqlite3.Connection, card: Mapping[str, object], unit: _Unit, now: datetime, tries: int
) -> None:
    """Stop replacing *unit*'s download, which failed for the same reason *tries* copies in a
    row, and write so to History, once."""
    with conn:
        stopped = conn.execute(
            "UPDATE arr_import_retries SET stopped_at=? WHERE key=? AND stopped_at IS NULL",
            (now.isoformat(), unit.key),
        ).rowcount
        if stopped:
            reason = f" ({unit.reason})" if unit.reason else ""
            log_audit(
                conn,
                unit.key,
                STOPPED_ACTION,
                f"{_name(_shown_of(card, unit))} kept failing for the same reason{reason} after"
                f" {tries} copies; mediaman stopped downloading it again until an admin tries again",
            )
    logger.info("import_retry.stopped key=%s tries=%d", unit.key, tries)


def stopped_words(conn: sqlite3.Connection, key: str) -> str:
    """What a card says of a title of *key*, as ``import_retry`` keys it, that mediaman stopped
    replacing; "" while it still tries. A *key* that ends in ":" is any season of a series."""
    try:
        row = conn.execute(
            "SELECT reason_tries FROM arr_import_retries WHERE key LIKE ? AND stopped_at IS NOT NULL"
            " LIMIT 1",
            (f"{key}%" if key.endswith(":") else key,),
        ).fetchone()
    except sqlite3.DatabaseError:
        logger.warning("Failed to read whether %s was given up", key, exc_info=True)
        return ""
    return "" if row is None else f"Stopped trying after {row[0]} copies failed the same way"


def try_again(conn: sqlite3.Connection, key: str) -> bool:
    """Have mediaman replace the stuck download of *key* again, as it stopped doing; a *key* that
    ends in ":" is every season of a series. Whether there was one it had stopped for."""
    with conn:
        return (
            conn.execute(
                "UPDATE arr_import_retries SET stopped_at=NULL, reason_tries=0"
                " WHERE key LIKE ? AND stopped_at IS NOT NULL",
                (f"{key}%" if key.endswith(":") else key,),
            ).rowcount
            > 0
        )


def _may_have_acted(exc: Exception) -> bool:
    """Whether a DELETE that failed with *exc* may still have been carried out: it was sent
    and its answer was lost, or the server failed while at it; not when it could not connect."""
    if isinstance(exc, SafeHTTPError) and exc.status_code == 0:
        # A transport error, raised from the one the request met.
        return not isinstance(exc.__cause__, requests.ConnectionError)
    if isinstance(exc, requests.RequestException):
        return not isinstance(exc, requests.ConnectionError)
    return isinstance(exc, SafeHTTPError) and exc.status_code >= 500


def _film_imported(client: Any, movie_id: int, since: datetime) -> bool | None:
    """Whether Radarr added the file film *movie_id* has after *since*; None when the film is
    gone."""
    try:
        movie = client._get(f"/api/v3/movie/{movie_id}", quick=True)
    except SafeHTTPError as exc:
        if exc.status_code == 404:
            return None
        raise
    file = movie.get("movieFile") if isinstance(movie, dict) and movie.get("hasFile") else None
    return isinstance(file, dict) and _after(file.get("dateAdded"), since)


def _season_imported(
    client: Any, key: str, episodes: set[int], since: datetime, shows: dict[int, bool]
) -> bool | None:
    """Whether Sonarr added a file of one of *episodes* of the season of *key*, of any of its
    episodes where none is named, after *since*; None when the show is gone. Whether each show
    is there is read once, in *shows*; of the rest, the season and its files alone."""
    series_id, season = _key_id(key), key.rpartition(":")[2]
    if series_id not in shows:
        try:
            client._get(f"/api/v3/series/{series_id}", quick=True)
            shows[series_id] = True
        except SafeHTTPError as exc:
            if exc.status_code != 404:
                raise
            shows[series_id] = False
    if not shows[series_id]:
        return None
    listed = client._get_list(
        f"/api/v3/episode?seriesId={series_id}&seasonNumber={season}", quick=True
    )
    files = [
        _int(e.get("episodeFileId"))
        for e in listed
        if e.get("hasFile") and (not episodes or _int(e.get("id")) in episodes)
    ]
    return any(_file_imported(client, file, since) for file in files if file)


def _file_imported(client: Any, file_id: int, since: datetime) -> bool:
    found = client._get(f"/api/v3/episodefile/{file_id}", quick=True)
    return isinstance(found, dict) and _after(found.get("dateAdded"), since)


def _after(text: object, since: datetime) -> bool:
    added = parse_iso_utc(text) if isinstance(text, str) else None
    return added is not None and added > since


def _forget_imported(conn: sqlite3.Connection, secret_key: str, now: datetime) -> None:
    """Forget each title tried before whose film, or an episode its last try was for, has a file
    added since that try began, so its tries start again: a file it had before, as of a copy that
    was not an upgrade, gives nothing back. Forget one gone from Radarr or Sonarr too, once the
    service answered for another title, as a wrong address answers 404 for every one.

    A search still to send keeps its row. Each title is asked with one short try; one whose read
    fails is asked again next run, and a service that gives no answer is not asked again in this
    one. Each run starts at another title, so one that never answers holds back no other for good.
    """
    clients: dict[str, Any] = {}
    shows: dict[int, bool] = {}
    down: set[str] = set()
    answered: set[str] = set()
    gone: list[tuple[str, str]] = []
    forget = "DELETE FROM arr_import_retries WHERE key=? AND tried_at=? AND search IS NULL"
    rows = conn.execute(
        "SELECT key, tried_at, tried_episodes FROM arr_import_retries"
        " WHERE tries > 0 AND search IS NULL AND tried_at IS NOT NULL ORDER BY key"
    ).fetchall()
    start = int(now.timestamp() // _STUCK_FOR.total_seconds()) % len(rows) if rows else 0
    for key, tried_at, episodes in rows[start:] + rows[:start]:
        service = key.partition(":")[0]
        began = parse_iso_utc(tried_at)
        if service in down or began is None:
            continue
        try:
            if service not in clients:
                clients[service] = _client(conn, secret_key, service)
            client = clients[service]
            if client is None:
                down.add(service)
                continue
            if service == "radarr":
                imported = _film_imported(client, _key_id(key), began)
            else:
                imported = _season_imported(client, key, _ids(episodes), began, shows)
        except _ERRORS as exc:
            if _no_answer(exc):
                down.add(service)
            logger.warning("Could not ask %s whether %s has imported", service, key, exc_info=True)
            continue
        if imported is None:
            gone.append((key, tried_at))
            continue
        answered.add(service)
        if imported:
            with conn:
                conn.execute(forget, (key, tried_at))
    for key, tried_at in gone:
        if key.partition(":")[0] in answered:
            with conn:
                conn.execute(forget, (key, tried_at))


def _expire(conn: sqlite3.Connection, now: datetime, cards: Sequence[Mapping[str, object]]) -> None:
    """Forget each title out of the queue of *cards* and not being tried that was last tried
    ``_KEPT_FOR`` ago, with its tries."""
    queued = {unit.key for _card, unit in _units(cards)}
    for key, tried_at in conn.execute(
        "SELECT key, tried_at FROM arr_import_retries WHERE tried_at IS NOT NULL"
        " AND stuck_since IS NULL AND retried_at IS NULL AND search IS NULL"
    ).fetchall():
        began = parse_iso_utc(tried_at)
        if key not in queued and began is not None and now - began >= _KEPT_FOR:
            with conn:
                conn.execute(
                    "DELETE FROM arr_import_retries WHERE key=? AND tried_at=?"
                    " AND stuck_since IS NULL AND retried_at IS NULL AND search IS NULL",
                    (key, tried_at),
                )


def retry_stuck_imports(
    conn: sqlite3.Connection,
    secret_key: str,
    cards: Sequence[Mapping[str, object]],
    searched: set[tuple[str, int]] | None = None,
) -> None:
    """Forget the titles that have imported since they were tried, send each search a run
    before could not, and remove and search again for each download of *cards* that has not
    imported for an hour, this last not while the setting is off. A service that gives no
    answer to a search or a removal is not sent another in this run.

    *searched* holds the films and shows, as ``(service, id)``, already searched for in this
    pass; each one this pass acts on is added, and one already there is left for the next run,
    so no title is searched for twice at once."""
    if searched is None:
        searched = set()
    # Seasons of one show are each this pass's own; another path's search holds them all back.
    before = frozenset(searched)
    _expire(conn, now_utc(), cards)
    _forget_imported(conn, secret_key, now_utc())
    down: set[str] = set()
    for key, search, shown, retried_at in conn.execute(
        "SELECT key, search, shown, retried_at FROM arr_import_retries"
        " WHERE retried_at IS NOT NULL AND search IS NOT NULL"
    ).fetchall():
        service = key.partition(":")[0]
        title = (service, _key_id(key))
        if service in down or title in before:
            continue
        searched.add(title)
        try:
            client = _client(conn, secret_key, service)
        except _ERRORS:
            client = None
        if client is None or _search(conn, client, key, search, shown, parse_iso_utc(retried_at)):
            down.add(service)
    if not get_bool_setting(conn, SETTING, default=True):
        return
    now = now_utc()
    replaced: dict[str, int] = {}
    for card, unit in _oldest_first(conn, _units(cards)):
        service = unit.key.partition(":")[0]
        # One held by the service's own setup, or by a hold that passes, is not acted on.
        if not unit.stuck or unit.mode not in stuck_modes.ACTIONABLE or service in down:
            continue
        title = (service, _key_id(unit.key))
        if title in before:
            continue
        # Whatever comes of its turn, nothing else searches for it in this pass.
        searched.add(title)
        may_replace = replaced.get(service, 0) < _REPLACED_PER_RUN
        came = _retry(conn, secret_key, card, unit, now, may_replace=may_replace)
        if came == _SILENT:
            down.add(service)
        elif came == _REPLACED:
            replaced[service] = replaced.get(service, 0) + 1


def _oldest_first(
    conn: sqlite3.Connection, units: list[tuple[Mapping[str, object], _Unit]]
) -> list[tuple[Mapping[str, object], _Unit]]:
    """*units*, those that have gone longest without importing first."""
    since = dict(
        conn.execute(
            "SELECT key, COALESCE(first_stuck_at, stuck_since, '') FROM arr_import_retries"
        ).fetchall()
    )
    return sorted(units, key=lambda pair: since.get(pair[1].key) or "\uffff")
