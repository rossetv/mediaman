"""A half-hourly look at Radarr and Sonarr: what is wrong with them, told in Activity in plain
words, and the little that is safe to mend, mended.

It reads each service's health, disk space, queue, failed downloads and library. It writes to a
service only two kinds of command, each capped and logged: a scan of a download whose setup
fault has cleared (DownloadedEpisodesScan, DownloadedMoviesScan), and a fresh look at a title
Plex has but the service lists as missing (RescanSeries, RefreshMovie). It deletes nothing and
never touches Plex or the files.

What it finds is kept in ``arr_care``, one row a thing, until the thing is gone; a check whose
read fails leaves its rows as they were.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import requests

from mediaman.core.time import now_utc, parse_iso_utc
from mediaman.services.arr._transport import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.stuck_modes import SETUP, classify
from mediaman.services.downloads.download_format import attention_reason
from mediaman.services.infra import ConfigDecryptError, SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)

# A health warning seen at the poll before this one: one poll alone may be a blip.
HEALTH_AFTER = timedelta(minutes=20)
# A setup fault or an unreachable download client is told after an hour.
FAULT_AFTER = timedelta(hours=1)
LOW_BYTES = 10 * 1000**3
LOW_SHARE = 0.05
FAILURES_OVER = 5
SCANS_AT_MOST = 3
REFRESHES_AT_MOST = 2
REFRESH_EVERY = timedelta(days=1)
# Commands one service is sent in one pass, whatever it holds.
COMMANDS_PER_PASS = 5
_TEXT_MAX = 200
_READ_ERRORS = (SafeHTTPError, requests.RequestException, ArrError, ValueError)
_QUOTED_PATH = re.compile(r"'(/[^']*)'")


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong, by its *key*: what Activity says of it once it has been seen for
    *after* (None says nothing), whether an admin must act, and whether a command went for it."""

    key: str
    line: str | None
    needs_you: bool = False
    after: timedelta = timedelta(0)
    tried: bool = False


@dataclass(slots=True)
class _Service:
    client: ArrClient
    id: str  # "sonarr" or "radarr"
    name: str
    items: str  # what it imports, plural
    folder: str
    now: datetime
    rows: dict[str, sqlite3.Row]
    budget: int = COMMANDS_PER_PASS

    def send(self, command: Mapping[str, Any], why: str) -> bool:
        """Send *command* if this pass's budget allows; whether it went."""
        if self.budget <= 0:
            logger.info("arr_care.command_held service=%s why=%s", self.id, why)
            return False
        self.budget -= 1
        try:
            self.client.send_command(command)
        except _READ_ERRORS:
            logger.warning("arr_care.command_failed service=%s why=%s", self.id, why, exc_info=True)
            return False
        logger.info(
            "arr_care.command_sent service=%s command=%s why=%s", self.id, command["name"], why
        )
        return True


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:_TEXT_MAX]


def _ago(svc: _Service, at: str | None) -> timedelta | None:
    when = parse_iso_utc(at) if at else None
    return svc.now - when if when else None


# ── Health ──


def _health_line(svc: _Service, source: str, level: str, message: str) -> tuple[str, timedelta]:
    lower = message.lower()
    if source.startswith("Indexer"):
        if level == "error" or "all indexers" in lower:
            return (
                f"{svc.name} can't reach any indexer, so nothing new will download.",
                HEALTH_AFTER,
            )
        return (
            f"{svc.name} can't reach some of its indexers, so it may find fewer copies.",
            HEALTH_AFTER,
        )
    if source.startswith("DownloadClient"):
        return f"{svc.name} can't reach the download client; downloads are on hold.", FAULT_AFTER
    if source == "RootFolderCheck":
        return (
            f"{svc.name} can't find a library folder, so {svc.items} can't be imported.",
            HEALTH_AFTER,
        )
    if source == "RemotePathMappingCheck":
        return (
            f"{svc.name} can't find the download client's files, so {svc.items} can't be imported.",
            HEALTH_AFTER,
        )
    return f"{svc.name} reports a problem: {message}", HEALTH_AFTER


def _health(svc: _Service) -> list[Finding]:
    found: dict[str, Finding] = {}
    for entry in svc.client._get_list("/api/v3/health"):
        level = str(entry.get("type") or "").lower()
        source = _text(entry.get("source")) or "health"
        message = _text(entry.get("message"))
        if level not in ("warning", "error") or not message:
            continue
        key = f"{svc.id}:health:{source}"
        if key not in found:
            line, after = _health_line(svc, source, level, message)
            found[key] = Finding(key, line, needs_you=True, after=after)
    return list(found.values())


# ── Disk space ──


def _gb(size: float) -> str:
    return "under 1 GB" if size < 1000**3 else f"{size / 1000**3:.0f} GB"


def _disk(svc: _Service) -> list[Finding]:
    roots = svc.client._get_list("/api/v3/rootfolder")
    disks = svc.client._get_list("/api/v3/diskspace")
    out = []
    for root in roots:
        path = str(root.get("path") or "")
        if not path:
            continue
        # The disk a folder is on is the one whose mount point is the longest start of its path.
        under = [d for d in disks if path.startswith(str(d.get("path") or "\0"))]
        disk = max(under, key=lambda d: len(str(d.get("path"))), default={})
        free = root.get("freeSpace")
        free = disk.get("freeSpace") if free is None else free
        total = disk.get("totalSpace")
        if not isinstance(free, int | float) or isinstance(free, bool):
            continue
        share = free / total if isinstance(total, int | float) and total > 0 else 1.0
        if free >= LOW_BYTES and share >= LOW_SHARE:
            continue
        named = f"Your {svc.folder} folder" + (f" {path}" if len(roots) > 1 else "")
        line = f"{named} has {_gb(free)} left; {svc.items} will stop importing soon."
        out.append(Finding(f"{svc.id}:disk:{path}", line, needs_you=True))
    return out


# ── The queue: setup faults and the download client ──


def _title_of(record: Mapping[str, Any]) -> str:
    owner = record.get("series") or record.get("movie")
    title = owner.get("title") if isinstance(owner, Mapping) else None
    return _text(title or record.get("title") or "a download")


def _fault_words(reason: str) -> str:
    lower = reason.lower()
    path = _QUOTED_PATH.search(reason)
    if "permission" in lower or "access to the path" in lower:
        return "no permission on " + (path.group(1) if path else "its folder")
    if "remote path mapping" in lower or "not a valid local path" in lower:
        return "it can't see the download client's folder"
    if "root folder missing" in lower:
        return "its library folder is missing"
    if "not enough free space" in lower:
        return "the disk is full"
    if "path does not exist" in lower or "not accessible" in lower:
        return "it can't reach " + (path.group(1) if path else "the download's folder")
    return reason.rstrip(". ")


def _queue(svc: _Service) -> list[Finding]:
    out: list[Finding] = []
    client_down = False
    scan = "DownloadedEpisodesScan" if svc.id == "sonarr" else "DownloadedMoviesScan"
    for record in svc.client.get_queue():
        rec: Mapping[str, Any] = record
        status = str(rec.get("status") or "")
        if status.lower() == "downloadclientunavailable":
            client_down = True
            continue
        download_id = str(rec.get("downloadId") or "")
        if not download_id:
            continue
        reason = attention_reason(rec)
        mode = classify(
            status,
            str(rec.get("trackedDownloadState") or ""),
            str(rec.get("trackedDownloadStatus") or ""),
            reason,
        )
        key = f"{svc.id}:queue:setup:{download_id}"
        title = _title_of(rec)
        if mode == SETUP:
            line = f"{svc.name} can't move {title} into your library: {_fault_words(reason)}."
            out.append(Finding(key, line, needs_you=True, after=FAULT_AFTER))
            continue
        row = svc.rows.get(key)
        if row is None or not row["told_at"]:
            continue
        # Told of a fault that has since cleared: the service is asked to import it again.
        tried = False
        path = str(rec.get("outputPath") or "")
        if row["tries"] < SCANS_AT_MOST and path:
            command = {
                "name": scan,
                "path": path,
                "downloadClientId": download_id,
                "importMode": "Auto",
            }
            tried = svc.send(command, f"setup fault cleared for {download_id}")
        told = f"mediaman asked {svc.name} to import {title} again." if tried else None
        out.append(Finding(key, told, tried=tried))
    if client_down:
        line = f"{svc.name} can't reach the download client; downloads are on hold."
        out.append(Finding(f"{svc.id}:queue:client", line, needs_you=True, after=FAULT_AFTER))
    return out


# ── Failed downloads ──


def _failing(svc: _Service) -> list[Finding]:
    own = "series" if svc.id == "sonarr" else "movie"
    records, _ = svc.client._get_page(
        f"/api/v3/history?eventType=4&page=1&pageSize=250&sortKey=date&sortDirection=descending"
        f"&include{own.capitalize()}=true"
    )
    since = svc.now - timedelta(days=1)
    counts: Counter[int] = Counter()
    titles: dict[int, str] = {}
    unpacking: Counter[int] = Counter()
    for rec in records:
        when = parse_iso_utc(str(rec.get("date") or ""))
        title_id = rec.get(f"{own}Id")
        if when is None or when < since or not isinstance(title_id, int):
            continue
        counts[title_id] += 1
        titles.setdefault(title_id, _title_of(rec))
        data = rec.get("data")
        said = str(data.get("message") or "") if isinstance(data, Mapping) else ""
        if "unpack" in said.lower() or "extract" in said.lower():
            unpacking[title_id] += 1
    out = []
    for title_id, n in counts.items():
        if n <= FAILURES_OVER:
            continue
        how = " to unpack" if unpacking[title_id] * 2 > n else ""
        line = (
            f"{titles[title_id]} downloads keep failing{how} ({n} today); "
            f"{svc.name} keeps trying other copies."
        )
        out.append(Finding(f"{svc.id}:failing:{title_id}", line, needs_you=True))
    return out


# ── The library: what Plex has that the service calls missing, and unmonitored shows ──


def _in_plex(conn: sqlite3.Connection, column: str) -> set[int]:
    kind = "media_type = 'movie'" if column == "radarr_id" else "media_type != 'movie'"
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM media_items WHERE {column} IS NOT NULL"
        f" AND unlisted = 0 AND {kind} AND COALESCE(file_size_bytes, 0) > 0"
    )
    return {int(r[0]) for r in rows}


def _refresh(svc: _Service, title_id: int, title: str, command: Mapping[str, Any]) -> Finding:
    key = f"{svc.id}:missing:{title_id}"
    row = svc.rows.get(key)
    tries = row["tries"] if row else 0
    since_last = _ago(svc, row["tried_at"]) if row else None
    due = since_last is None or since_last >= REFRESH_EVERY
    if tries >= REFRESHES_AT_MOST and due:
        line = f"Plex has {title} but {svc.name} still lists it as missing; check where {svc.name} looks for it."
        return Finding(key, line, needs_you=True)
    tried = due and svc.send(command, f"Plex has {svc.id} {title_id}")
    line = f"Plex has {title} but {svc.name} listed it as missing; mediaman asked {svc.name} to look again."
    return Finding(key, line, tried=tried)


def _radarr_library(conn: sqlite3.Connection) -> Callable[[_Service], list[Finding]]:
    def check(svc: _Service) -> list[Finding]:
        held = _in_plex(conn, "radarr_id")
        out = []
        for movie in svc.client.get_movies():
            movie_id = movie.get("id")
            if movie_id in held and movie.get("monitored") and movie.get("hasFile") is False:
                command = {"name": "RefreshMovie", "movieIds": [movie_id]}
                out.append(_refresh(svc, int(movie_id), _text(movie.get("title")), command))
        return out

    return check


def _sonarr_library(conn: sqlite3.Connection) -> Callable[[_Service], list[Finding]]:
    def check(svc: _Service) -> list[Finding]:
        held = _in_plex(conn, "sonarr_id")
        out = []
        for series in svc.client.get_series():
            series_id = series.get("id")
            if not isinstance(series_id, int):
                continue
            if not series.get("monitored"):
                out.append(Finding(f"sonarr:unmonitored:{series_id}", None))
                continue
            stats = series.get("statistics")
            stats = stats if isinstance(stats, Mapping) else {}
            wanted = stats.get("episodeCount")
            if series_id in held and stats.get("episodeFileCount") == 0 and wanted:
                command = {"name": "RescanSeries", "seriesId": series_id}
                out.append(_refresh(svc, series_id, _text(series.get("title")), command))
        return out

    return check


# ── Keeping what was found ──


def _load(conn: sqlite3.Connection, prefix: str) -> dict[str, sqlite3.Row]:
    cur = conn.execute("SELECT * FROM arr_care WHERE key >= ? AND key < ?", (prefix, prefix + "￿"))
    cur.row_factory = sqlite3.Row
    return {row["key"]: row for row in cur}


def _settle(
    conn: sqlite3.Connection, svc: _Service, prefixes: Iterable[str], found: list[Finding]
) -> None:
    now = svc.now.isoformat()
    keep = {f.key for f in found}
    for f in found:
        row = svc.rows.get(f.key)
        first = row["first_seen"] if row else now
        seen_for = _ago(svc, first) or timedelta(0)
        line = f.line if seen_for >= f.after else None
        told = (row["told_at"] if row else None) or (now if line else None)
        tries = (row["tries"] if row else 0) + (1 if f.tried else 0)
        tried_at = now if f.tried else (row["tried_at"] if row else None)
        conn.execute(
            "INSERT INTO arr_care (key, first_seen, line, needs_you, told_at, tries, tried_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET line = excluded.line,"
            " needs_you = excluded.needs_you, told_at = excluded.told_at,"
            " tries = excluded.tries, tried_at = excluded.tried_at",
            (f.key, first, line, int(f.needs_you), told, tries, tried_at),
        )
    for prefix in prefixes:
        for key in [k for k in svc.rows if k.startswith(prefix) and k not in keep]:
            conn.execute("DELETE FROM arr_care WHERE key = ?", (key,))
    conn.commit()


def _care_for(
    conn: sqlite3.Connection, svc: _Service, library: Callable[[_Service], list[Finding]]
) -> None:
    checks: list[tuple[tuple[str, ...], Callable[[_Service], list[Finding]]]] = [
        (("health:",), _health),
        (("disk:",), _disk),
        (("queue:",), _queue),
        (("failing:",), _failing),
        (("missing:", "unmonitored:"), library),
    ]
    for parts, check in checks:
        prefixes = [f"{svc.id}:{p}" for p in parts]
        svc.rows = {k: r for p in prefixes for k, r in _load(conn, p).items()}
        try:
            found = check(svc)
        except _READ_ERRORS:
            logger.warning(
                "arr_care.read_failed service=%s check=%s", svc.id, parts[0], exc_info=True
            )
            continue
        _settle(conn, svc, prefixes, found)


def look_after_arrs(
    conn: sqlite3.Connection, secret_key: str, *, now: datetime | None = None
) -> None:
    """The scheduler's half-hourly pass over every Radarr and Sonarr set up."""
    at = now or now_utc()
    services = (
        ("sonarr", "Sonarr", "new episodes", "TV", build_sonarr_from_db, _sonarr_library(conn)),
        ("radarr", "Radarr", "new films", "film", build_radarr_from_db, _radarr_library(conn)),
    )
    for sid, name, items, folder, build, library in services:
        try:
            client = build(conn, secret_key)
        except ConfigDecryptError:
            logger.warning("arr_care.key_unreadable service=%s", sid)
            continue
        if client is None:
            conn.execute("DELETE FROM arr_care WHERE key >= ? AND key < ?", (sid + ":", sid + ":￿"))
            conn.commit()
            continue
        _care_for(conn, _Service(client, sid, name, items, folder, at, {}), library)


def care_lines(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """What Activity says, each line once: those that need an admin first."""
    rows = conn.execute(
        "SELECT line, MAX(needs_you) FROM arr_care WHERE line IS NOT NULL"
        " GROUP BY line ORDER BY MAX(needs_you) DESC, MIN(first_seen), line"
    )
    return [{"line": line, "needs_you": bool(needs)} for line, needs in rows]


def unmonitored_series(conn: sqlite3.Connection) -> set[int]:
    """The ids of the Sonarr series the last pass found not monitored."""
    rows = conn.execute(
        "SELECT key FROM arr_care WHERE key >= 'sonarr:unmonitored:' AND key < 'sonarr:unmonitored;'"
    )
    return {int(key.rpartition(":")[2]) for (key,) in rows}
