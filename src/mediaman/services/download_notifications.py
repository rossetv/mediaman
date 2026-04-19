"""Download completion notifications.

Polls Radarr/Sonarr for download requests that now have files and emails
the requester a "ready to watch" notification via Mailgun. Designed to be
called frequently (once per library sync) so users get timely alerts.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("mediaman")


def check_download_notifications(conn: sqlite3.Connection, secret_key: str) -> None:
    """Send completion emails for downloads that are now available in Plex.

    Queries ``download_notifications`` for un-notified rows, checks whether
    the item now has a file in Radarr/Sonarr, and sends a simple email via
    Mailgun if so.  Marks the row as notified=1 to prevent duplicate sends.

    This is designed to be called from the library sync job so it runs
    frequently enough that users get a timely notification.
    """
    from jinja2 import Environment, FileSystemLoader

    from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
    from mediaman.services.mailgun import MailgunClient
    from mediaman.services.settings_reader import get_string_setting

    pending = conn.execute(
        "SELECT id, email, title, media_type, tmdb_id, tvdb_id, service "
        "FROM download_notifications WHERE notified=0"
    ).fetchall()
    if not pending:
        return

    # Build Mailgun client — bail early if not configured
    mailgun_domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    mailgun_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    mailgun_from = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    if not mailgun_domain or not mailgun_key:
        logger.debug("Download notifications skipped — Mailgun not configured")
        return
    mailgun = MailgunClient(mailgun_domain, mailgun_key, mailgun_from)

    # Build *arr clients once, lazily — avoid paying the HTTP cost when the
    # queue only contains movies (or only TV)
    _radarr = None
    _sonarr = None
    _radarr_built = False
    _sonarr_built = False

    def get_radarr():
        nonlocal _radarr, _radarr_built
        if not _radarr_built:
            _radarr = build_radarr_from_db(conn, secret_key)
            _radarr_built = True
        return _radarr

    def get_sonarr():
        nonlocal _sonarr, _sonarr_built
        if not _sonarr_built:
            _sonarr = build_sonarr_from_db(conn, secret_key)
            _sonarr_built = True
        return _sonarr

    # Load the email template
    template_dir = Path(__file__).parent.parent / "web" / "templates"
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
                client = get_radarr()
                if client and tmdb_id:
                    movie = client.get_movie_by_tmdb(tmdb_id)
                    ready = bool(movie and movie.get("hasFile"))
            elif service == "sonarr":
                client = get_sonarr()
                # Match on TVDB id first (authoritative for Sonarr); fall
                # back to TMDB for series added via TMDB lookup where the
                # Sonarr record happens to carry both.
                if client and (tvdb_id or tmdb_id):
                    for s in client.get_series():
                        if tvdb_id and s.get("tvdbId") == tvdb_id:
                            stats = s.get("statistics") or {}
                            ready = stats.get("episodeFileCount", 0) > 0
                            break
                        if tmdb_id and s.get("tmdbId") == tmdb_id:
                            stats = s.get("statistics") or {}
                            ready = stats.get("episodeFileCount", 0) > 0
                            break

            if not ready:
                continue

            # Gather rich metadata for the email
            meta = {"year": "", "runtime": "", "director": "", "description": "",
                    "rating": "", "imdb_rating": "", "rt_rating": "", "poster_url": ""}

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
                        for img in movie.get("images") or []:
                            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                                meta["poster_url"] = img["remoteUrl"]
                                break
                except Exception:
                    pass

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

            conn.execute(
                "UPDATE download_notifications SET notified=1 WHERE id=?", (row_id,)
            )
            conn.commit()
            logger.info("Download notification sent to %s for '%s'", email, title)

        except Exception:
            logger.exception("Failed to process download notification id=%s for '%s'", row_id, title)
