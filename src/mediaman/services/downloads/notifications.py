# rationale: this file is the orchestration layer for download notifications;
# the three private submodules (_notification_backoff, _notification_claims,
# _notification_email) already hold the decomposed helpers, leaving only glue
# code here — a further split would distribute a single pipeline across four or
# more files with no cohesive concept to name the extra module.
"""Download completion notifications.

Polls Radarr/Sonarr for download requests that now have files and emails
the requester a "ready to watch" notification via Mailgun. Designed to be
called frequently (once per library sync) so users get timely alerts.
"""

from __future__ import annotations

import enum
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

import requests

from mediaman.core.time import now_utc
from mediaman.services.arr._types import RadarrMovie
from mediaman.services.arr.base import ArrError
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from jinja2 import Template

    from mediaman.services.arr._types import SonarrSeries
    from mediaman.services.arr.base import ArrClient
    from mediaman.services.arr.state import LazyArrClients
    from mediaman.services.mail.mailgun import MailgunClient

from mediaman.services.downloads._notification_backoff import (
    _clear_backoff,
    _is_backed_off,
    _record_arr_failure,
    _record_poll_attempt,
)
from mediaman.services.downloads._notification_claims import (
    STRANDED_CLAIM_GRACE_SECONDS,
    ClaimedNotificationRow,
    _claim_pending_notifications,
    _release_claim,
    _release_claims_bulk,
    reconcile_stranded_notifications,
)
from mediaman.services.downloads._notification_email import (
    SuggestionsMeta,
)
from mediaman.services.downloads._notification_email import (
    build_email_payload as _build_email_payload,
)
from mediaman.services.downloads._notification_email import (
    get_notification_template as _get_notification_template,
)

logger = logging.getLogger(__name__)


def record_download_notification(
    conn: sqlite3.Connection,
    *,
    email: str,
    title: str,
    media_type: str,
    service: str,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> None:
    """Insert a pending notification; the library sync sends it once the item has a file.

    Radarr uses TMDB ids and Sonarr TVDB ids, so each goes in its own column. Does not
    commit; the caller owns the transaction.
    """
    from mediaman.core.time import now_iso

    now = now_iso()
    conn.execute(
        "INSERT INTO download_notifications "
        "(email, title, media_type, tmdb_id, tvdb_id, service, notified, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (email, title, media_type, tmdb_id, tvdb_id, service, now),
    )


def _build_sonarr_index(client: ArrClient) -> list[SonarrSeries]:
    """Fetch the full Sonarr library once per tick; every pending row reuses it instead of
    making its own HTTP call.
    """
    return client.get_series()


def _build_radarr_index(client: ArrClient) -> dict[int, RadarrMovie]:
    """Fetch the full Radarr library once per tick as a {tmdbId: movie} index, avoiding a
    per-row HTTP round-trip.
    """
    index: dict[int, RadarrMovie] = {}
    for movie in client.get_movies():
        tid = movie.get("tmdbId")
        if tid:
            index[int(tid)] = movie
    return index


def _sonarr_has_files(
    series_index: list[SonarrSeries],
    *,
    tvdb_id: int | None,
    tmdb_id: int | None,
) -> bool:
    """Return True if the series has an episode file, matching by TVDB id first and then
    TMDB id. *series_index* is pre-fetched once per tick.
    """
    for s in series_index:
        if tvdb_id and s.get("tvdbId") == tvdb_id:
            stats = s.get("statistics") or {}
            return int(stats.get("episodeFileCount") or 0) > 0
        if tmdb_id and s.get("tmdbId") == tmdb_id:
            stats = s.get("statistics") or {}
            return int(stats.get("episodeFileCount") or 0) > 0
    return False


def _build_mailgun_client(
    conn: sqlite3.Connection,
    secret_key: str,
    pending: list[ClaimedNotificationRow],
) -> MailgunClient | None:
    """Build a MailgunClient, or None (releasing all pending claims in bulk) when Mailgun
    is not configured. An empty from-address counts as misconfigured because most MTAs
    reject an empty From header.
    """
    from mediaman.services.infra import get_string_setting
    from mediaman.services.mail.mailgun import MailgunClient

    mailgun_domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    mailgun_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    mailgun_from = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    if not mailgun_domain or not mailgun_key or not mailgun_from:
        logger.debug("Download notifications skipped — Mailgun not configured")
        # Single bulk UPDATE + single commit instead of N round-trips.
        # On a backlog of dozens of pending rows the per-row path used
        # to issue one fsync each.
        _release_claims_bulk(conn, [r.id for r in pending])
        return None
    return MailgunClient(mailgun_domain, mailgun_key, mailgun_from)


def _partition_runnable(
    conn: sqlite3.Connection,
    pending: list[ClaimedNotificationRow],
    now_dt: datetime,
) -> list[ClaimedNotificationRow]:
    """Split *pending* into deferred (backed-off) and runnable rows, releasing the deferred
    ones in bulk.
    """
    # Filter out rows currently in backoff so a sticky *arr outage does
    # not turn into N claim/release cycles per tick. Released back to
    # ``notified=0`` in bulk so the next tick can pick them up if their
    # backoff has elapsed.
    deferred_ids: list[int] = []
    runnable: list[ClaimedNotificationRow] = []
    for row in pending:
        if _is_backed_off(row.id, now_dt):
            deferred_ids.append(row.id)
        else:
            runnable.append(row)
    if deferred_ids:
        _release_claims_bulk(conn, deferred_ids)
    return runnable


def _fetch_suggestions_batch(
    conn: sqlite3.Connection,
    runnable: list[ClaimedNotificationRow],
) -> dict[int, SuggestionsMeta]:
    """Return a tmdb_id to SuggestionsMeta map from one query, skipped when no row has a
    tmdb_id (an empty IN () is a SQLite syntax error).
    """
    # Batch the suggestions lookup so an N-row tick only fires one query
    # instead of N. Skips when no row has a tmdb_id so we don't run a
    # ``WHERE tmdb_id IN ()`` (which is a syntax error in SQLite).
    tmdb_ids = sorted({r.tmdb_id for r in runnable if r.tmdb_id is not None})
    suggestions_by_tmdb: dict[int, SuggestionsMeta] = {}
    if tmdb_ids:
        # rationale: placeholder list built from integer TMDB IDs only; no user input reaches the SQL string
        placeholders = ",".join("?" * len(tmdb_ids))
        sugg_rows = conn.execute(
            f"SELECT tmdb_id, year, runtime, director, description, rating, "
            f"imdb_rating, rt_rating, poster_url "
            f"FROM suggestions WHERE tmdb_id IN ({placeholders})",
            tmdb_ids,
        ).fetchall()
        for sr in sugg_rows:
            # Multiple suggestion rows may exist for the same tmdb_id
            # across batches — keep the first hit (any row contains the
            # same metadata aside from rating timing).
            tid = int(sr["tmdb_id"])
            if tid not in suggestions_by_tmdb:
                suggestions_by_tmdb[tid] = SuggestionsMeta(
                    tmdb_id=tid,
                    year=str(sr["year"]) if sr["year"] else "",
                    runtime=str(sr["runtime"]) if sr["runtime"] else "",
                    director=str(sr["director"]) if sr["director"] else "",
                    description=str(sr["description"]) if sr["description"] else "",
                    rating=str(sr["rating"]) if sr["rating"] else "",
                    imdb_rating=str(sr["imdb_rating"]) if sr["imdb_rating"] else "",
                    rt_rating=str(sr["rt_rating"]) if sr["rt_rating"] else "",
                    poster_url=str(sr["poster_url"]) if sr["poster_url"] else "",
                )
    return suggestions_by_tmdb


class _ArrProbeOutcome(enum.Enum):
    """Sentinel outcomes of _check_arr_availability; an enum so identity checks type
    correctly.
    """

    UNREACHABLE = "unreachable"  # arr lookup failed; claim released, backoff applied
    UNKNOWN_SERVICE = "unknown_service"  # service field not "radarr"/"sonarr"; row drained


_ARR_UNREACHABLE = _ArrProbeOutcome.UNREACHABLE
_ARR_UNKNOWN_SERVICE = _ArrProbeOutcome.UNKNOWN_SERVICE


@dataclass(frozen=True, slots=True)
class RadarrProbeResult:
    ready: bool
    movie: RadarrMovie | None
    arr_unreachable: bool


def _check_radarr_movie(
    arr: LazyArrClients,
    row_id: int,
    tmdb_id: int | None,
    radarr_index: dict[int, RadarrMovie] | None = None,
) -> RadarrProbeResult:
    """Probe Radarr for one movie's file availability.

    Network and HTTP errors surface as arr_unreachable=True so the caller can back off
    and release the claim. With *radarr_index* the lookup is a dict access, and None in
    the index means Radarr does not know the movie. Without an index it falls back to a
    per-row HTTP call.
    """
    radarr_client = arr.radarr()
    if not (radarr_client and tmdb_id):
        return RadarrProbeResult(ready=False, movie=None, arr_unreachable=False)

    if radarr_index is not None:
        # O(1) index lookup — no HTTP round-trip.
        movie = radarr_index.get(tmdb_id)
        return RadarrProbeResult(
            ready=bool(movie and movie.get("hasFile")), movie=movie, arr_unreachable=False
        )

    try:
        movie = radarr_client.get_movie_by_tmdb(tmdb_id)
    # rationale: transient outage / backoff — network blips, 5xx responses,
    # malformed JSON, and Radarr-specific errors must all surface as
    # arr_unreachable=True so the caller releases the claim and applies backoff.
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning(
            "Radarr lookup failed for notification id=%s tmdb=%s",
            row_id,
            tmdb_id,
            exc_info=True,
        )
        return RadarrProbeResult(ready=False, movie=None, arr_unreachable=True)
    return RadarrProbeResult(
        ready=bool(movie and movie.get("hasFile")), movie=movie, arr_unreachable=False
    )


def _check_sonarr_series(
    arr: LazyArrClients,
    row_id: int,
    tvdb_id: int | None,
    tmdb_id: int | None,
    sonarr_series_index: list[SonarrSeries] | None,
) -> tuple[bool, bool]:
    """Probe Sonarr for a series' episode-file availability, returning (ready,
    arr_unreachable). *sonarr_series_index* is the library list pre-fetched once per
    tick.
    """
    sonarr_client = arr.sonarr()
    # Match on TVDB id first (authoritative for Sonarr); fall back
    # to TMDB for series added via TMDB lookup where the Sonarr
    # record happens to carry both.
    if not (sonarr_client and (tvdb_id or tmdb_id)):
        return False, False
    # rationale: transient outage / backoff — symmetric with the Radarr path;
    # network blips, 5xx responses, malformed JSON, and Sonarr-specific errors
    # become arr_unreachable=True so the caller releases the claim and applies backoff.
    try:
        index = (
            sonarr_series_index
            if sonarr_series_index is not None
            else _build_sonarr_index(sonarr_client)
        )
        ready = _sonarr_has_files(index, tvdb_id=tvdb_id, tmdb_id=tmdb_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning(
            "Sonarr lookup failed for notification id=%s",
            row_id,
            exc_info=True,
        )
        return False, True
    return ready, False


def _check_arr_availability(
    row: ClaimedNotificationRow,
    arr: LazyArrClients,
    now_dt: datetime,
    conn: sqlite3.Connection,
    sonarr_series_index: list[SonarrSeries] | None = None,
    radarr_index: dict[int, RadarrMovie] | None = None,
) -> tuple[bool, RadarrMovie | _ArrProbeOutcome | None]:
    """Check Radarr/Sonarr for file availability, returning (ready, movie_obj).

    movie_obj is a RadarrMovie (Radarr branch), None (no Radarr match, or Sonarr),
    _ArrProbeOutcome.UNREACHABLE (lookup failed; claim released, backoff applied) or
    UNKNOWN_SERVICE (service is neither radarr nor sonarr; row drained).

    Pre-fetch the indexes once per tick to avoid O(N x library) HTTP calls. The radarr
    and sonarr branches each have their own try/except; splitting further would separate
    an except from its try.
    """
    row_id = row.id
    service = row.service

    if service == "radarr":
        result = _check_radarr_movie(arr, row_id, row.tmdb_id, radarr_index)
        ready, movie, arr_unreachable = result.ready, result.movie, result.arr_unreachable
    elif service == "sonarr":
        ready, arr_unreachable = _check_sonarr_series(
            arr, row_id, row.tvdb_id, row.tmdb_id, sonarr_series_index
        )
        movie = None
    else:
        # Unknown service value — drain the row so the scheduler doesn't spin
        # forever on an unrecognisable service string (e.g. DB corruption or
        # a future service type this version doesn't know about).
        logger.warning(
            "Download notification id=%s has unknown service=%r — marking as notified "
            "to drain the queue rather than spinning indefinitely",
            row_id,
            service,
        )
        conn.execute(
            "UPDATE download_notifications SET notified=1 WHERE id=?",
            (row_id,),
        )
        conn.commit()
        return False, _ARR_UNKNOWN_SERVICE

    if arr_unreachable:
        _record_arr_failure(row_id, now_dt)
        _release_claim(conn, row_id)
        return False, _ARR_UNREACHABLE

    return ready, movie


def _process_one_notification(
    conn: sqlite3.Connection,
    row: ClaimedNotificationRow,
    arr: LazyArrClients,
    mailgun: MailgunClient,
    template: Template,
    suggestions_by_tmdb: dict[int, SuggestionsMeta],
    now_dt: datetime,
    sonarr_series_index: list[SonarrSeries] | None = None,
    radarr_index: dict[int, RadarrMovie] | None = None,
) -> None:
    """Check availability, send the email and mark one claimed row.

    Releases the claim back to notified=0 on any failure so a later tick retries.
    Genuine arr failures get exponential backoff; items still downloading get the short
    poll throttle.
    """
    row_id = row.id
    email = row.email
    title = row.title

    try:
        ready, movie = _check_arr_availability(
            row,
            arr,
            now_dt,
            conn,
            sonarr_series_index=sonarr_series_index,
            radarr_index=radarr_index,
        )

        if movie is _ARR_UNREACHABLE:
            # _check_arr_availability already recorded the failure and
            # released the claim — nothing left to do for this row.
            return

        if movie is _ARR_UNKNOWN_SERVICE:
            # _check_arr_availability already drained the row.
            return

        if not ready:
            # Item still downloading — release the claim so the next
            # tick of the scheduler can re-evaluate it.  Use a short
            # poll throttle (not the *arr-failure backoff) so a long
            # download doesn't accumulate exponential delay on a healthy
            # connection.  The backoff counter is deliberately NOT bumped
            # here; it is reserved for genuine *arr reachability failures.
            _record_poll_attempt(int(row_id), now_dt)
            _release_claim(conn, row_id)
            return

        # Sentinels are exhausted above; cast narrows the union for mypy
        # without touching runtime behaviour.
        movie_typed = cast("RadarrMovie | None", movie)
        subject, html = _build_email_payload(row, movie_typed, suggestions_by_tmdb, template)
        mailgun.send(to=email, subject=subject, html=html)

        conn.execute("UPDATE download_notifications SET notified=1 WHERE id=?", (row_id,))
        conn.commit()
        # Successful send — drop any backoff/poll-throttle state we may
        # have built up for this row during a previous outage or wait.
        _clear_backoff(int(row_id))
        logger.info("Download notification sent to %s for '%s'", email, title)

    except (SafeHTTPError, requests.RequestException, ArrError, sqlite3.Error):
        # rationale: outer boundary — scheduler must survive a single bad row;
        # covers Mailgun transport failures (SafeHTTPError, RequestException),
        # Radarr/Sonarr errors (ArrError), and DB write failures (sqlite3.Error).
        logger.exception("Failed to process download notification id=%s for '%s'", row_id, title)
        # Mailgun (or another downstream) failed — apply genuine *arr backoff
        # so a Mailgun-down period doesn't burn N tries per minute.
        _record_arr_failure(int(row_id), now_dt)
        # Release the claim so a later scheduler tick can retry —
        # otherwise a transient Mailgun outage strands the row at
        # ``notified=2`` forever.
        _release_claim(conn, row_id)


def check_download_notifications(conn: sqlite3.Connection, secret_key: str) -> None:
    """Send completion emails for downloads now available in Plex.

    Per tick: reset stranded notified=2 rows, atomically claim un-notified rows, check
    Radarr/Sonarr for a file and send via Mailgun, then mark notified=1, or release the
    claim back to notified=0 if the item is not ready or the send fails. Called from the
    library sync job.
    """
    from mediaman.services.arr.state import LazyArrClients

    pending = _claim_pending_notifications(conn)
    if not pending:
        return

    mailgun = _build_mailgun_client(conn, secret_key, pending)
    if mailgun is None:
        return

    # Build *arr clients once, lazily — avoid paying the HTTP cost when the
    # queue only contains movies (or only TV).
    arr = LazyArrClients(conn, secret_key)

    # Reuse the module-cached Jinja env + compiled template so a tick
    # with many pending rows doesn't pay the FS-walk + parse cost on
    # every invocation.
    template = _get_notification_template()

    now_dt = now_utc()
    runnable = _partition_runnable(conn, pending, now_dt)
    if not runnable:
        return

    suggestions_by_tmdb = _fetch_suggestions_batch(conn, runnable)

    # Build the Sonarr series index once per tick so _sonarr_has_files is O(1)
    # per row instead of O(N × library) (M1).  Fetch only when at least one
    # runnable row needs Sonarr; Radarr rows pay nothing.
    sonarr_series_index: list[SonarrSeries] | None = None
    if any(r.service == "sonarr" for r in runnable):
        sonarr_client = arr.sonarr()
        if sonarr_client:
            try:
                sonarr_series_index = _build_sonarr_index(sonarr_client)
            except (SafeHTTPError, requests.RequestException, ArrError):
                logger.warning("Failed to pre-fetch Sonarr series index", exc_info=True)
                # sonarr_series_index stays None; _check_sonarr_series will
                # call _build_sonarr_index per-row as a safe fallback.

    # Build the Radarr movie index once per tick — avoids one HTTP round-trip
    # per Radarr row (R4-H1: N+1 fix mirrors the existing Sonarr optimisation).
    radarr_index: dict[int, RadarrMovie] | None = None
    if any(r.service == "radarr" for r in runnable):
        radarr_client = arr.radarr()
        if radarr_client:
            try:
                radarr_index = _build_radarr_index(radarr_client)
            except (SafeHTTPError, requests.RequestException, ArrError):
                logger.warning("Failed to pre-fetch Radarr movie index", exc_info=True)
                # radarr_index stays None; _check_radarr_movie falls back to
                # per-row get_movie_by_tmdb as a safe fallback.

    for row in runnable:
        _process_one_notification(
            conn,
            row,
            arr,
            mailgun,
            template,
            suggestions_by_tmdb,
            now_dt,
            sonarr_series_index=sonarr_series_index,
            radarr_index=radarr_index,
        )


__all__ = [
    "STRANDED_CLAIM_GRACE_SECONDS",
    "check_download_notifications",
    "reconcile_stranded_notifications",
    "record_download_notification",
]
