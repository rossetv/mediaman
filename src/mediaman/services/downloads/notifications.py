"""Download completion notifications.

Polls Radarr/Sonarr for download requests that now have files and emails
the requester a "ready to watch" notification via Mailgun. Designed to be
called frequently (once per library sync) so users get timely alerts.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.infra.time import now_iso

logger = logging.getLogger("mediaman")

#: How long an in-flight claim is allowed before reconcile treats the row as
#: stranded by a crashed worker (H-5).  Generous enough to outlast the
#: slowest legitimate notify pipeline (Mailgun retries, slow SMTP), short
#: enough that a stranded row is recovered on the next service restart
#: rather than waiting for the operator to notice.
STRANDED_CLAIM_GRACE_SECONDS = 3600


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
                    f"UPDATE download_notifications SET notified=2, claimed_at=? WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are ? only
                    (claim_iso, *ids),
                )
            conn.execute("COMMIT")
            return rows
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
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
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat()
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
    from jinja2 import Environment, FileSystemLoader

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
        for row in pending:
            _release_claim(conn, row["id"])
        return
    mailgun = MailgunClient(mailgun_domain, mailgun_key, mailgun_from)

    # Build *arr clients once, lazily — avoid paying the HTTP cost when the
    # queue only contains movies (or only TV).
    arr = LazyArrClients(conn, secret_key)

    template_dir = Path(__file__).parent.parent.parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("email/download_ready.html")

    for row in pending:
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
            if service == "radarr":
                client = arr.radarr()
                if client and tmdb_id:
                    movie = client.get_movie_by_tmdb(tmdb_id)
                    ready = bool(movie and movie.get("hasFile"))
            elif service == "sonarr":
                client = arr.sonarr()
                # Match on TVDB id first (authoritative for Sonarr); fall
                # back to TMDB for series added via TMDB lookup where the
                # Sonarr record happens to carry both.
                if client and (tvdb_id or tmdb_id):
                    ready = _sonarr_has_files(client, tvdb_id=tvdb_id, tmdb_id=tmdb_id)

            if not ready:
                # Item still downloading — release the claim so the next
                # tick of the scheduler can re-evaluate it.
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

            # Try recommendations table first
            rec_row = conn.execute(
                "SELECT year, runtime, director, description, rating, imdb_rating, "
                "rt_rating, poster_url FROM suggestions WHERE tmdb_id = ? LIMIT 1",
                (tmdb_id,),
            ).fetchone()
            if rec_row:
                for k in meta:
                    if rec_row[k]:
                        meta[k] = rec_row[k]

            # Fall back to Radarr/Sonarr for poster if missing
            if not meta["poster_url"]:
                try:
                    if service == "radarr" and movie:
                        url = extract_poster_url(movie.get("images"))
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
            logger.info("Download notification sent to %s for '%s'", email, title)

        except Exception:
            logger.exception(
                "Failed to process download notification id=%s for '%s'", row_id, title
            )
            # Release the claim so a later scheduler tick can retry —
            # otherwise a transient Mailgun outage strands the row at
            # ``notified=2`` forever.
            _release_claim(conn, row_id)
