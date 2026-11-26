"""Downloads Radarr or Sonarr will not import, removed and searched for again unasked.

A film's download, or a Sonarr season's, that has needed attention for an hour is removed
from the download client with its release blocklisted, and the same film or episodes are
searched for again. Each film or season is given ``MAX_TRIES`` copies until Radarr or Sonarr
adds a file of it after a try; one that will not import after that is left as it is, for an
admin, as is one held by the service's own setup rather than by its release, or one whose
removal the service refuses. While another copy is looked for, its card says so. Library
files and Plex are never touched, nor a download still coming in or one importing as it
should.

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

# What Sonarr and Radarr say of a download their own setup keeps from importing (paths, remote
# path mappings, permissions, the root folder, disk space): another copy would not import either.
_SETUP_PROBLEMS = (
    "remote path mapping",
    "not a valid local path",
    "path does not exist",
    "not accessible",
    "permission",
    "access to the path",
    "root folder missing",
    "not enough free space",
)


@dataclass(frozen=True, slots=True)
class _Unit:
    """A film's queue record, or the records of one season of a show, tried as one."""

    key: str
    stuck: bool
    # The records that need attention, which alone are removed.
    held_ids: frozenset[int]
    # The episodes of the records that need attention; none of a film's.
    episodes: frozenset[int]
    queue_ids: frozenset[int]
    label: str
    reason: str
    search: dict[str, Any]
    setup_problem: bool


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


def _unit(
    key: str, entries: Sequence[Mapping[str, object]], label: str, search: dict[str, Any]
) -> _Unit:
    held = [e for e in entries if _held(e)]
    reasons = [str(e.get("attention") or "") for e in held]
    return _Unit(
        key=key,
        stuck=map_queue_state(entries) == "needs_attention" and bool(held),
        held_ids=frozenset(_int(e.get("queue_id")) for e in held),
        episodes=frozenset(_int(e.get("episode_id")) for e in held) - {0},
        queue_ids=frozenset(_int(e.get("queue_id")) for e in entries),
        label=label,
        reason=next((r for r in reasons if r), ""),
        search=search,
        setup_problem=any(p in r.lower() for r in reasons for p in _SETUP_PROBLEMS),
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
        units.append((records[0], _unit(f"radarr:{movie_id}", records, "", search)))
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
        enabled = get_bool_setting(conn, SETTING, default=True)
        rows = {
            row[0]: row
            for row in conn.execute(
                "SELECT key, stuck_since, retried_at, removed, shown, tries, search"
                " FROM arr_import_retries WHERE key LIKE ?",
                (f"{service}:%",),
            )
        }
        writes: list[tuple[str, tuple[object, ...]]] = []
        seen: set[str] = set()
        reset = (
            "UPDATE arr_import_retries SET stuck_since=?, retried_at=NULL, removed=NULL,"
            " search=NULL, shown=NULL WHERE key=?"
        )
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
                writes.append(
                    (
                        "INSERT INTO arr_import_retries (key, stuck_since) VALUES (?, ?)",
                        (unit.key, now),
                    )
                )
            elif row is not None and unit.stuck and row[1] is None:
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


def _history_line(shown: Mapping[str, Any], tries: int) -> str:
    name = f"'{shown.get('title') or 'A download'}'"
    if shown.get("label"):
        name += f" {shown['label']}"
    reason = f" ({shown['reason']})" if shown.get("reason") else ""
    return (
        f"{name} couldn't be imported{reason}; removed and searching for another copy"
        f" (try {tries} of {MAX_TRIES})"
    )


def _refused(exc: Exception) -> bool:
    return isinstance(exc, SafeHTTPError) and 400 <= exc.status_code < 500


def _search(
    conn: sqlite3.Connection,
    client: Any,
    key: str,
    search: str,
    shown: str,
    since: datetime | None,
) -> bool:
    """Send the search of a removed download, and write its try to History once it is sent;
    one the service has refused since a removal *since* ``_FINDING_FOR`` ago is given up.
    Whether the service gave no answer."""
    try:
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
        return not refused
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


def _retry(
    conn: sqlite3.Connection,
    secret_key: str,
    card: Mapping[str, object],
    unit: _Unit,
    now: datetime,
) -> bool:
    """Remove *unit*'s records and search again, once it has been stuck long enough and has
    tries left. Whether its service gave no answer."""
    row = conn.execute(
        "SELECT stuck_since, tries FROM arr_import_retries WHERE key=? AND retried_at IS NULL",
        (unit.key,),
    ).fetchone()
    since = parse_iso_utc(row[0]) if row is not None and row[0] else None
    if since is None or row[1] >= MAX_TRIES or now - since < _STUCK_FOR - _POLL_SLACK:
        return False
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
        # Taken by the one run that changes the row, should two ever run at once; the try counts
        # from here, as the removal may go through whatever is heard of it.
        claimed = conn.execute(
            "UPDATE arr_import_retries SET stuck_since=NULL, tries=tries + 1, retried_at=?,"
            " removed=?, search=?, shown=?, tried_at=?, tried_episodes=?"
            " WHERE key=? AND stuck_since=? AND retried_at IS NULL AND tries=?",
            (
                now.isoformat(),
                json.dumps(sorted(unit.held_ids)),
                search,
                shown,
                now.isoformat(),
                json.dumps(sorted(unit.episodes)),
                unit.key,
                row[0],
                row[1],
            ),
        ).rowcount
    if claimed != 1:
        return False
    removed_any = False
    try:
        client = _client(conn, secret_key, service)
        if client is None:
            raise ArrError(f"{service} is not set up")
        for queue_id in sorted(unit.held_ids):
            client.remove_from_queue(queue_id)
            removed_any = True
    except _ERRORS as exc:
        refused = _refused(exc)
        if removed_any or _may_have_acted(exc):
            # Removed with no search of its own: the search must still go, now or next run.
            logger.warning(
                "Could not remove all of %s's download that would not import",
                unit.key,
                exc_info=True,
            )
            _search(conn, client, unit.key, search, shown, now)
            return not refused
        # Refused outright, it would be refused again: its tries are spent, for an admin.
        then = "leaving it as it is" if refused else "trying again on the next run"
        logger.warning(
            "Could not remove %s's download that would not import; %s",
            unit.key,
            then,
            exc_info=True,
        )
        with conn:
            conn.execute(
                "UPDATE arr_import_retries SET stuck_since=?, tries=?, retried_at=NULL,"
                " removed=NULL, search=NULL, shown=NULL WHERE key=?",
                (row[0], MAX_TRIES if refused else row[1], unit.key),
            )
        return not refused
    return _search(conn, client, unit.key, search, shown, now)


def _may_have_acted(exc: Exception) -> bool:
    """Whether a DELETE that failed with *exc* may still have been carried out: it was sent
    and its answer was lost, or the server failed while at it; not when it could not connect."""
    if isinstance(exc, SafeHTTPError) and exc.status_code == 0:
        # A transport error, raised from the one the request met.
        return not isinstance(exc.__cause__, requests.ConnectionError)
    if isinstance(exc, requests.RequestException):
        return not isinstance(exc, requests.ConnectionError)
    return isinstance(exc, SafeHTTPError) and exc.status_code >= 500


def _film_added(client: Any, movie_id: int) -> list[str] | None:
    """When Radarr added the file film *movie_id* has, if any; None when the film is gone."""
    try:
        movie = client._get(f"/api/v3/movie/{movie_id}", quick=True)
    except SafeHTTPError as exc:
        if exc.status_code == 404:
            return None
        raise
    file = movie.get("movieFile") if isinstance(movie, dict) and movie.get("hasFile") else None
    return [str(file.get("dateAdded") or "")] if isinstance(file, dict) else []


def _show_added(client: Any, series_id: int) -> dict[int, str] | None:
    """When Sonarr added the file of each episode of show *series_id* that has one, by the
    episode's id; None when the show is gone."""
    try:
        client._get(f"/api/v3/series/{series_id}", quick=True)
    except SafeHTTPError as exc:
        if exc.status_code == 404:
            return None
        raise
    files = {
        _int(e.get("id")): _int(e.get("episodeFileId"))
        for e in client._get_list(f"/api/v3/episode?seriesId={series_id}", quick=True)
        if e.get("hasFile")
    }
    if not files:
        return {}
    added = {
        _int(f.get("id")): str(f.get("dateAdded") or "")
        for f in client._get_list(f"/api/v3/episodefile?seriesId={series_id}", quick=True)
    }
    return {episode: added.get(file, "") for episode, file in files.items()}


def _forget_imported(conn: sqlite3.Connection, secret_key: str, down: set[str]) -> None:
    """Forget each title tried before whose film, or an episode its last try was for, has a file
    added since that try began, so its tries start again: a file it had before, as of a copy that
    was not an upgrade, gives nothing back. Forget one gone from Radarr or Sonarr too, once the
    service answered for another title, as a wrong address answers 404 for every one.

    A search still to send keeps its row. Each service is asked with one short try, and not
    again in this run once it fails, its name then in *down*; a show is read once.
    """
    clients: dict[str, Any] = {}
    shows: dict[int, dict[int, str] | None] = {}
    answered: set[str] = set()
    gone: list[tuple[str, str]] = []
    forget = "DELETE FROM arr_import_retries WHERE key=? AND tried_at=? AND search IS NULL"
    for key, tried_at, episodes in conn.execute(
        "SELECT key, tried_at, tried_episodes FROM arr_import_retries"
        " WHERE tries > 0 AND search IS NULL AND tried_at IS NOT NULL"
    ).fetchall():
        service = key.partition(":")[0]
        if service in down:
            continue
        try:
            if service not in clients:
                clients[service] = _client(conn, secret_key, service)
            client = clients[service]
            if client is None:
                down.add(service)
                continue
            if service == "radarr":
                dates = _film_added(client, _key_id(key))
            else:
                series_id = _key_id(key)
                if series_id not in shows:
                    shows[series_id] = _show_added(client, series_id)
                show = shows[series_id]
                dates = None if show is None else [show.get(e, "") for e in _ids(episodes)]
        except _ERRORS:
            down.add(service)
            logger.warning(
                "Could not ask %s whether %s has imported; not asking it again this run",
                service,
                key,
                exc_info=True,
            )
            continue
        if dates is None:
            gone.append((key, tried_at))
            continue
        answered.add(service)
        began = parse_iso_utc(tried_at)
        if began is not None and any(
            (added := parse_iso_utc(d)) is not None and added > began for d in dates
        ):
            with conn:
                conn.execute(forget, (key, tried_at))
    for key, tried_at in gone:
        if key.partition(":")[0] in answered:
            with conn:
                conn.execute(forget, (key, tried_at))


def retry_stuck_imports(
    conn: sqlite3.Connection, secret_key: str, cards: Sequence[Mapping[str, object]]
) -> None:
    """Forget the titles that have imported since they were tried, send each search a run
    before could not, and remove and search again for each download of *cards* that has not
    imported for an hour, this last not while the setting is off. A service that gives no
    answer is not asked again in this run."""
    down: set[str] = set()
    _forget_imported(conn, secret_key, down)
    for key, search, shown, retried_at in conn.execute(
        "SELECT key, search, shown, retried_at FROM arr_import_retries"
        " WHERE retried_at IS NOT NULL AND search IS NOT NULL"
    ).fetchall():
        service = key.partition(":")[0]
        if service in down:
            continue
        try:
            client = _client(conn, secret_key, service)
        except _ERRORS:
            client = None
        if client is None or _search(conn, client, key, search, shown, parse_iso_utc(retried_at)):
            down.add(service)
    if not get_bool_setting(conn, SETTING, default=True):
        return
    now = now_utc()
    for card, unit in _units(cards):
        service = unit.key.partition(":")[0]
        stuck = unit.stuck and not unit.setup_problem and service not in down
        if stuck and _retry(conn, secret_key, card, unit, now):
            down.add(service)
