"""Downloads Radarr or Sonarr will not import, removed and searched for again unasked.

A film's download, or a Sonarr season's, that has needed attention for an hour is removed
from the download client with its release blocklisted, and the same film or episodes are
searched for again. Each film or season is given ``MAX_TRIES`` copies until Radarr or Sonarr
adds a file of it after a try; one that will not import after that is left as it is, for an
admin, as is one held by the service's own setup rather than by its release, or one whose
removal the service refuses; one the service holds for a while and imports by itself is waited
for. While another copy is looked for, its card says so. Library files and Plex are never
touched, nor a download still coming in or one importing as it should. Where the service
searches again by itself on a removal, as older ones do, mediaman sends no search of its own.

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
from mediaman.services.downloads.download_format._classify import not_the_release
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
# A title out of the queue, last tried this long ago, is forgotten with its tries.
_KEPT_FOR = timedelta(days=30)

_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ConfigDecryptError, ValueError)


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
    # Held by the service's own setup, or by a hold it lifts by itself, not by its release.
    not_the_release: bool


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
        downloads={_int(e.get("queue_id")): str(e.get("download_id") or "") for e in held},
        episodes=frozenset(_int(e.get("episode_id")) for e in held) - {0},
        queue_ids=frozenset(_int(e.get("queue_id")) for e in entries),
        label=label,
        reason=next((r for r in reasons if r), ""),
        search=search,
        not_the_release=any(not_the_release(r) for r in reasons),
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
    # A record already gone was failed by nobody here, so no search of the service's own
    # follows unless it went with a download this run removed.
    removed_downloads: set[str] = set()
    every_removed = True
    client = None
    try:
        client = _client(conn, secret_key, service)
        if client is None:
            raise ArrError(f"{service} is not set up")
        for queue_id in sorted(unit.held_ids):
            download = unit.downloads.get(queue_id, "")
            if client.remove_from_queue(queue_id):
                removed_downloads.add(download)
            elif not download or download not in removed_downloads:
                every_removed = False
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
            return silent
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
        return silent
    return _search(conn, client, unit.key, search, shown, now, every_removed)


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
    conn: sqlite3.Connection, secret_key: str, cards: Sequence[Mapping[str, object]]
) -> None:
    """Forget the titles that have imported since they were tried, send each search a run
    before could not, and remove and search again for each download of *cards* that has not
    imported for an hour, this last not while the setting is off. A service that gives no
    answer to a search or a removal is not sent another in this run."""
    _expire(conn, now_utc(), cards)
    _forget_imported(conn, secret_key, now_utc())
    down: set[str] = set()
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
        stuck = unit.stuck and not unit.not_the_release and service not in down
        if stuck and _retry(conn, secret_key, card, unit, now):
            down.add(service)
