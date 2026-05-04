"""Download completion notifications.

Polls Radarr/Sonarr for download requests that now have files and emails
the requester a "ready to watch" notification via Mailgun. Designed to be
called frequently (once per library sync) so users get timely alerts.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra.backoff import ExponentialBackoff
from mediaman.services.infra.time import now_iso

logger = logging.getLogger("mediaman")

#: How long an in-flight claim is allowed before reconcile treats the row as
#: stranded by a crashed worker (H-5).  Generous enough to outlast the
#: slowest legitimate notify pipeline (Mailgun retries, slow SMTP), short
#: enough that a stranded row is recovered on the next service restart
#: rather than waiting for the operator to notice.
STRANDED_CLAIM_GRACE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Module-cached Jinja environment.
#
# ``check_download_notifications`` is called once per library sync. Building
# a fresh ``Environment`` (filesystem walk + template compilation) every tick
# was wasted work, so we cache one env per process and reuse it for every
# call. The download-ready template lives next to the newsletter templates
# under ``mediaman/web/templates`` and never changes at runtime.
# ---------------------------------------------------------------------------
_NOTIFICATION_ENV = None
_NOTIFICATION_TEMPLATE = None


def _get_notification_template():
    """Return the cached ``email/download_ready.html`` Jinja template.

    Built lazily on first use rather than at import time so unit tests
    that never trigger this code path don't pay the Jinja import cost.
    """
    global _NOTIFICATION_ENV, _NOTIFICATION_TEMPLATE
    if _NOTIFICATION_TEMPLATE is not None:
        return _NOTIFICATION_TEMPLATE
    from jinja2 import Environment, FileSystemLoader

    template_dir = Path(__file__).parent.parent.parent / "web" / "templates"
    _NOTIFICATION_ENV = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    _NOTIFICATION_TEMPLATE = _NOTIFICATION_ENV.get_template("email/download_ready.html")
    return _NOTIFICATION_TEMPLATE


# ---------------------------------------------------------------------------
# In-process backoff for transient *arr outages.
#
# When Radarr/Sonarr is unreachable (or returns ready=False) the original
# loop claimed every pending row, found nothing ready, and released — once
# per scheduler tick. With a sticky outage and N pending rows that's N×
# claim/release cycles per minute for nothing. We keep an in-memory
# ``next_retry_at`` per row id and skip the row while its backoff is
# active.  The state is process-local — a restart wipes it (which matches
# our existing reconcile-on-startup story).
# ---------------------------------------------------------------------------
_BACKOFF_BASE_SECONDS = 60.0  # first retry waits 1 minute
_BACKOFF_MAX_SECONDS = 1800.0  # cap at 30 minutes
_NOTIFY_BACKOFF = ExponentialBackoff(_BACKOFF_BASE_SECONDS, _BACKOFF_MAX_SECONDS)
_backoff_state: dict[int, tuple[int, datetime]] = {}


def _is_backed_off(row_id: int, now: datetime) -> bool:
    """Return True if *row_id* should be skipped this tick due to backoff."""
    record = _backoff_state.get(row_id)
    if record is None:
        return False
    _attempts, next_retry_at = record
    return now < next_retry_at


def _record_arr_failure(row_id: int, now: datetime) -> None:
    """Bump the backoff counter for *row_id* and schedule the next retry."""
    attempts, _next = _backoff_state.get(row_id, (0, now))
    attempts += 1
    delay = _NOTIFY_BACKOFF.delay(attempts)
    _backoff_state[row_id] = (attempts, now + timedelta(seconds=delay))


def _clear_backoff(row_id: int) -> None:
    """Forget the backoff record for *row_id* once it has cleared."""
    _backoff_state.pop(row_id, None)


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
    now = now_iso()
    conn.execute(
        "INSERT INTO download_notifications "
        "(email, title, media_type, tmdb_id, tvdb_id, service, notified, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (email, title, media_type, tmdb_id, tvdb_id, service, now),
    )


def _sonarr_has_files(client, *, tvdb_id: int | None, tmdb_id: int | None) -> bool:
    """Return True if the Sonarr series has at least one episode file.

    Matches by TVDB id first (authoritative for Sonarr), then falls back to
    TMDB id for series added via a TMDB lookup where both IDs are present.
    """
    for s in client.get_series():
        if tvdb_id and s.get("tvdbId") == tvdb_id:
            return (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
        if tmdb_id and s.get("tmdbId") == tmdb_id:
            return (s.get("statistics") or {}).get("episodeFileCount", 0) > 0
    return False


def _claim_pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Atomically claim every un-notified notification row.

    Uses ``UPDATE ... WHERE notified=0 RETURNING`` so a sibling worker
    (or a re-entrant scheduler tick — finding 22) cannot pick up the
    same row a second time. SQLite has supported the RETURNING clause
    since 3.35, which is comfortably older than the project's
    ``sqlite3`` floor.

    Returns the claimed rows in the same shape the previous SELECT
    returned, so the caller's row-handling code stays unchanged. On a
    SQLite build without RETURNING we fall back to the old
    SELECT-then-UPDATE flow inside an IMMEDIATE transaction so the
    write lock blocks any concurrent claim.
    """
    claim_iso = now_iso()
    try:
        rows = conn.execute(
            "UPDATE download_notifications SET notified=2, claimed_at=? "
            "WHERE notified=0 "
            "RETURNING id, email, title, media_type, tmdb_id, tvdb_id, service",
            (claim_iso,),
        ).fetchall()
        conn.commit()
        return rows
    except sqlite3.OperationalError:
        # Older SQLite without RETURNING — fall back to lock-then-claim.
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, email, title, media_type, tmdb_id, tvdb_id, service "
                "FROM download_notifications WHERE notified=0"
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE download_notifications SET notified=2, claimed_at=? WHERE id IN ({placeholders})",
                    (claim_iso, *ids),
                )
            conn.execute("COMMIT")
            return rows
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise


def _release_claim(conn: sqlite3.Connection, row_id: int) -> None:
    """Roll a claimed row back to ``notified=0`` so a future tick can retry.

    Used when the early-bail conditions inside :func:`check_download_notifications`
    fail (e.g. Mailgun later turns out to be unreachable for a specific
    item) — without this the row would stay stuck at ``notified=2``
    indefinitely.

    Clears ``claimed_at`` along with the status so a subsequent reconcile
    sweep does not see a phantom in-flight stamp on a row that is queued.
    """
    try:
        conn.execute(
            "UPDATE download_notifications SET notified=0, claimed_at=NULL WHERE id=?",
            (row_id,),
        )
        conn.commit()
    except Exception:
        logger.warning("failed to release notification claim id=%s", row_id, exc_info=True)


def _release_claims_bulk(conn: sqlite3.Connection, row_ids: list[int]) -> None:
    """Roll many claimed rows back to ``notified=0`` in a single statement.

    The per-row :func:`_release_claim` ran one ``UPDATE`` + one
    ``COMMIT`` per stranded row.  When Mailgun is unconfigured every
    pending row goes through the release path on every tick — that's N
    fsyncs per scheduler poke. This helper does the same work in a
    single statement and a single commit.

    Skips silently when ``row_ids`` is empty so callers can pipe the
    "claimed" list straight in.
    """
    if not row_ids:
        return
    try:
        placeholders = ",".join("?" * len(row_ids))
        conn.execute(
            f"UPDATE download_notifications SET notified=0, claimed_at=NULL "
            f"WHERE id IN ({placeholders})",
            row_ids,
        )
        conn.commit()
    except Exception:
        logger.warning(
            "failed to bulk-release notification claims (n=%d)", len(row_ids), exc_info=True
        )


def reconcile_stranded_notifications(
    conn: sqlite3.Connection,
    *,
    grace_seconds: int = STRANDED_CLAIM_GRACE_SECONDS,
) -> int:
    """Reset rows stranded at ``notified=2`` after a crashed worker (H-5).

    The atomic claim added for finding 22 prevents two workers from sending
    the same notification, but it does so by flipping ``notified=0 → 2``
    *before* the actual mail attempt.  An OOM, container restart, or
    SIGKILL between the claim and the send leaves rows pinned at
    ``notified=2`` forever — the in-process release path inside the
    sender loop only fires on Python exceptions.

    Call this once on startup (the FastAPI lifespan does so).  Rows whose
    ``claimed_at`` is older than *grace_seconds* are reset back to
    ``notified=0`` with ``claimed_at`` cleared so the next scheduler tick
    picks them up.  Returns the number of rows reset.

    *grace_seconds* is generous enough that a legitimate slow Mailgun
    pipeline isn't reaped — it is only ever observed by the next process
    after a restart, by which point the previous in-flight call is gone.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat()
    cur = conn.execute(
        "UPDATE download_notifications "
        "SET notified=0, claimed_at=NULL "
        "WHERE notified=2 "
        "  AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    )
    conn.commit()
    reset = cur.rowcount or 0
    if reset:
        logger.info("notifications.reconcile reset=%d cutoff=%s", reset, cutoff)
    return reset


def check_download_notifications(conn: sqlite3.Connection, secret_key: str) -> None:
    """Send completion emails for downloads that are now available in Plex.

    Queries ``download_notifications`` for un-notified rows, claims them
    atomically (finding 22), checks whether the item now has a file in
    Radarr/Sonarr, and sends a simple email via Mailgun if so. Marks the
    row as ``notified=1`` after a successful send; rolls back to 0 if
    the item isn't actually ready yet so a future scheduler tick can
    retry.

    This is designed to be called from the library sync job so it runs
    frequently enough that users get a timely notification.
    """
    from mediaman.services.arr.state import LazyArrClients
    from mediaman.services.infra.settings_reader import get_string_setting
    from mediaman.services.mail.mailgun import MailgunClient

    pending = _claim_pending_notifications(conn)
    if not pending:
        return

    # Build Mailgun client — bail early if not configured. Release the
    # claim on every pending row first so a future tick can retry once
    # Mailgun is wired up. Without this every row would be stuck at
    # ``notified=2`` until an operator notices.
    mailgun_domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    mailgun_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    mailgun_from = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    if not mailgun_domain or not mailgun_key:
        logger.debug("Download notifications skipped — Mailgun not configured")
        # Single bulk UPDATE + single commit instead of N round-trips.
        # On a backlog of dozens of pending rows the per-row path used
        # to issue one fsync each.
        _release_claims_bulk(conn, [int(r["id"]) for r in pending])
        return
    mailgun = MailgunClient(mailgun_domain, mailgun_key, mailgun_from)

    # Build *arr clients once, lazily — avoid paying the HTTP cost when the
    # queue only contains movies (or only TV).
    arr = LazyArrClients(conn, secret_key)

    # Reuse the module-cached Jinja env + compiled template so a tick
    # with many pending rows doesn't pay the FS-walk + parse cost on
    # every invocation.
    template = _get_notification_template()

    # Filter out rows currently in backoff so a sticky *arr outage does
    # not turn into N claim/release cycles per tick. Released back to
    # ``notified=0`` in bulk so the next tick can pick them up if their
    # backoff has elapsed.
    now_dt = datetime.now(UTC)
    deferred_ids: list[int] = []
    runnable: list[sqlite3.Row] = []
    for row in pending:
        if _is_backed_off(int(row["id"]), now_dt):
            deferred_ids.append(int(row["id"]))
        else:
            runnable.append(row)
    if deferred_ids:
        _release_claims_bulk(conn, deferred_ids)
    if not runnable:
        return

    # Batch the suggestions lookup so an N-row tick only fires one query
    # instead of N. Skips when no row has a tmdb_id so we don't run a
    # ``WHERE tmdb_id IN ()`` (which is a syntax error in SQLite).
    tmdb_ids = sorted({int(r["tmdb_id"]) for r in runnable if r["tmdb_id"] is not None})
    suggestions_by_tmdb: dict[int, sqlite3.Row] = {}
    if tmdb_ids:
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
            if sr["tmdb_id"] not in suggestions_by_tmdb:
                suggestions_by_tmdb[int(sr["tmdb_id"])] = sr

    for row in runnable:
        row_id = row["id"]
        email = row["email"]
        title = row["title"]
        media_type = row["media_type"]
        tmdb_id = row["tmdb_id"]
        # ``tvdb_id`` may not be present on very old DB rows created before
        # the v11 migration, but the ``SELECT`` above always aliases the
        # column so ``row["tvdb_id"]`` is defined — just possibly NULL.
        tvdb_id = row["tvdb_id"]
        service = row["service"]

        try:
            ready = False
            movie = None
            arr_unreachable = False
            if service == "radarr":
                radarr_client = arr.radarr()
                if radarr_client and tmdb_id:
                    try:
                        movie = radarr_client.get_movie_by_tmdb(tmdb_id)
                    except Exception:
                        # Network/HTTP errors propagate from Radarr — treat
                        # as a transient outage so the backoff kicks in.
                        arr_unreachable = True
                        logger.warning(
                            "Radarr lookup failed for notification id=%s tmdb=%s",
                            row_id,
                            tmdb_id,
                            exc_info=True,
                        )
                    else:
                        ready = bool(movie and movie.get("hasFile"))
            elif service == "sonarr":
                sonarr_client = arr.sonarr()
                # Match on TVDB id first (authoritative for Sonarr); fall
                # back to TMDB for series added via TMDB lookup where the
                # Sonarr record happens to carry both.
                if sonarr_client and (tvdb_id or tmdb_id):
                    try:
                        ready = _sonarr_has_files(sonarr_client, tvdb_id=tvdb_id, tmdb_id=tmdb_id)
                    except Exception:
                        arr_unreachable = True
                        logger.warning(
                            "Sonarr lookup failed for notification id=%s",
                            row_id,
                            exc_info=True,
                        )

            if arr_unreachable:
                _record_arr_failure(int(row_id), now_dt)
                _release_claim(conn, row_id)
                continue

            if not ready:
                # Item still downloading — release the claim so the next
                # tick of the scheduler can re-evaluate it.  Bump the
                # backoff so a long-running download doesn't burn a
                # claim/release every minute either.
                _record_arr_failure(int(row_id), now_dt)
                _release_claim(conn, row_id)
                continue

            # Gather rich metadata for the email
            meta = {
                "year": "",
                "runtime": "",
                "director": "",
                "description": "",
                "rating": "",
                "imdb_rating": "",
                "rt_rating": "",
                "poster_url": "",
            }

            # Recommendations cache lookup — pulled from the batch fetch
            # above so we don't do per-row queries.
            rec_row = suggestions_by_tmdb.get(int(tmdb_id)) if tmdb_id else None
            if rec_row:
                for k in meta:
                    if rec_row[k]:
                        meta[k] = rec_row[k]

            # Fall back to Radarr/Sonarr for poster if missing
            if not meta["poster_url"]:
                try:
                    if service == "radarr" and movie:
                        images = movie.get("images")
                        url = extract_poster_url(images if isinstance(images, list) else None)
                        if url:
                            meta["poster_url"] = url
                except (TypeError, KeyError):
                    logger.warning(
                        "Failed to extract Radarr poster for notification", exc_info=True
                    )

            media_label = "Movie" if media_type == "movie" else "TV"

            # Pass structured data to the template so Jinja's autoescape can
            # safely escape every user/TMDB-sourced field. Building raw HTML
            # strings in Python and rendering them with |safe risks XSS when
            # any TMDB field (e.g. director free-text) contains markup.
            meta_ctx = {
                "year": str(meta["year"]) if meta["year"] else "",
                "media_label": media_label,
                "runtime": meta["runtime"],
                "director": meta["director"],
            }
            ratings_ctx = {
                "rating": meta["rating"],
                "imdb_rating": meta["imdb_rating"],
                "rt_rating": meta["rt_rating"],
            }

            # Poster source — constrained to 240px height in the template.
            # Upgrade small TMDB posters to w500 for better-looking emails.
            poster_src = meta["poster_url"]
            if poster_src:
                poster_src = poster_src.replace("/w300", "/w500").replace("/w200", "/w500")

            subject = f"'{title}' is now available to watch"
            html = template.render(
                title=title,
                poster_src=poster_src,
                meta=meta_ctx,
                ratings=ratings_ctx,
                description=meta["description"],
            )

            mailgun.send(to=email, subject=subject, html=html)

            conn.execute("UPDATE download_notifications SET notified=1 WHERE id=?", (row_id,))
            conn.commit()
            # Successful send — drop any backoff state we may have built
            # up for this row during a previous outage.
            _clear_backoff(int(row_id))
            logger.info("Download notification sent to %s for '%s'", email, title)

        except Exception:
            logger.exception(
                "Failed to process download notification id=%s for '%s'", row_id, title
            )
            # Mailgun (or another downstream) failed — apply the same
            # backoff as for *arr outages so a Mailgun-down period
            # doesn't burn N tries per minute either.
            _record_arr_failure(int(row_id), now_dt)
            # Release the claim so a later scheduler tick can retry —
            # otherwise a transient Mailgun outage strands the row at
            # ``notified=2`` forever.
            _release_claim(conn, row_id)
