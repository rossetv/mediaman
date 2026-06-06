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

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

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
    """Insert a pending download notification record.

    The notification is sent by the library sync job once the item has a file
    in Radarr/Sonarr — i.e. when it's actually available to watch.

    Radarr uses TMDB IDs; Sonarr uses TVDB IDs. Store each in the matching
    column so the completion checker can match the right field on each
    service's response.

    Does **not** call ``conn.commit()`` — callers manage their own transactions.
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
    """Fetch the full Sonarr series library once and return it.

    Called at most once per ``check_download_notifications`` tick and the
    result is reused by every pending row (M1 — avoids O(N × library) HTTP).
    """
    return client.get_series()


def _sonarr_has_files(
    series_index: list[SonarrSeries],
    *,
    tvdb_id: int | None,
    tmdb_id: int | None,
) -> bool:
    """Return True if the Sonarr series has at least one episode file.

    Matches by TVDB id first (authoritative for Sonarr), then falls back to
    TMDB id for series added via a TMDB lookup where both IDs are present.

    ``series_index`` must be pre-fetched by the caller (once per scheduler
    tick) rather than being re-fetched per row.
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
    """Build a MailgunClient from settings, or ``None`` if Mailgun is not configured.

    When Mailgun is not configured (missing domain, key, or from-address),
    releases all pending claims in bulk and returns ``None`` so the caller
    can bail early.  An empty ``mailgun_from_address`` is treated as
    misconfigured — sending with an empty ``From`` header is rejected by
    most MTAs.
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
    """Split *pending* into deferred (backed-off) and runnable rows.

    Releases deferred rows in bulk and returns only the rows that are
    eligible to be processed this tick.
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
    """Return a tmdb_id → :class:`SuggestionsMeta` mapping fetched in a single query.

    Skips the query entirely when no runnable row has a tmdb_id (avoids a
    ``WHERE tmdb_id IN ()`` syntax error in SQLite).  Converts raw DB rows to
    :class:`SuggestionsMeta` at the boundary so the raw ``sqlite3.Row`` never
    crosses into ``_notification_email.py`` (§9.5).
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


_ARR_UNREACHABLE = object()  # sentinel returned by _check_arr_availability on outage
_ARR_UNKNOWN_SERVICE = object()  # sentinel for an unrecognised service value


@dataclass(frozen=True, slots=True)
class RadarrProbeResult:
    """Result of probing Radarr for a single movie's file availability.

    Replaces the raw ``(ready, movie, arr_unreachable)`` 3-tuple (§5.9).
    """

    ready: bool
    movie: RadarrMovie | None
    arr_unreachable: bool


def _check_radarr_movie(arr: LazyArrClients, row_id: int, tmdb_id: int | None) -> RadarrProbeResult:
    """Probe Radarr for a single movie's file availability.

    Returns a :class:`RadarrProbeResult`.  Network/HTTP errors from Radarr
    surface as ``arr_unreachable=True`` so the caller can apply backoff and
    release the claim.
    """
    radarr_client = arr.radarr()
    if not (radarr_client and tmdb_id):
        return RadarrProbeResult(ready=False, movie=None, arr_unreachable=False)
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
    """Probe Sonarr for a series' episode-file availability.

    Returns ``(ready, arr_unreachable)``.  Sonarr does not return a
    movie payload so the second element matches the Radarr helper's
    return shape via the caller's tuple unpacking.

    ``sonarr_series_index`` is the pre-fetched library list (built once per
    scheduler tick).  When ``None`` the client is not yet available.
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
) -> tuple[bool, RadarrMovie | None | Literal[_ARR_UNREACHABLE, _ARR_UNKNOWN_SERVICE]]:  # type: ignore[valid-type]
    """Check Radarr/Sonarr for file availability.

    Returns ``(ready, movie_obj)``.  The second element of the tuple is one of:

    * a :class:`~mediaman.services.arr._types.RadarrMovie` (Radarr branch)
    * ``None`` (no Radarr match, or Sonarr branch)
    * :data:`_ARR_UNREACHABLE` — *arr lookup failed; claim released, backoff applied.
    * :data:`_ARR_UNKNOWN_SERVICE` — service field is not "radarr"/"sonarr"; row drained.

    ``sonarr_series_index`` should be pre-fetched once per tick and passed in
    to avoid O(N × library) HTTP calls (M1).

    # rationale: two separate service branches (radarr/sonarr) each with
    # their own try/except — splitting further would separate the except
    # from its try across function boundaries.
    """
    row_id = row.id
    service = row.service

    if service == "radarr":
        result = _check_radarr_movie(arr, row_id, row.tmdb_id)
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
) -> None:
    """Check availability, send email, and mark one claimed notification row.

    Releases the claim back to ``notified=0`` on any failure so a later
    tick can retry.  Applies *arr-failure backoff on genuine reachability
    failures; uses a short in-progress poll throttle for items still
    downloading (keeps the two failure modes distinct — B1).

    ``sonarr_series_index`` should be the pre-fetched Sonarr library list
    (built once per tick in the orchestrator) to avoid per-row HTTP fetches.
    """
    row_id = row.id
    email = row.email
    title = row.title

    try:
        ready, movie = _check_arr_availability(
            row, arr, now_dt, conn, sonarr_series_index=sonarr_series_index
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
        # rationale: §6.4 outer boundary — scheduler must survive a single bad row;
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
    """Send completion emails for downloads that are now available in Plex.

    Queries ``download_notifications`` for un-notified rows, claims them
    atomically so concurrent scheduler ticks cannot pick up the same row,
    checks whether the item now has a file in Radarr/Sonarr, and sends a
    simple email via Mailgun if so. Marks the row as ``notified=1`` after a
    successful send; rolls back to ``notified=0`` if the item isn't actually
    ready yet so a future scheduler tick can retry.

    This is designed to be called from the library sync job so it runs
    frequently enough that users get a timely notification.

    Pipeline:
    1. Reset stranded ``notified=2`` rows whose claim has expired.
    2. Atomically claim un-notified rows (set ``notified=2``, stamp ``claimed_at``).
    3. For each claimed row, check Plex availability via Sonarr/Radarr.
    4. Send the email via Mailgun.
    5. On success, mark ``notified=1``; on failure, release the claim back to ``notified=0``.
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
        )


__all__ = [
    "STRANDED_CLAIM_GRACE_SECONDS",
    "check_download_notifications",
    "reconcile_stranded_notifications",
    "record_download_notification",
]
