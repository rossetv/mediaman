"""Email rendering helpers for download-ready notifications.

Owns the "build the Jinja template, gather TMDB/Sonarr/Radarr metadata,
and render the email payload" concern.  Split from
:mod:`mediaman.services.downloads.notifications` so the orchestration
file stays scannable; the orchestrator drives DB claims, *arr probes,
and Mailgun dispatch only.

Threat model
------------
Every TMDB-sourced field is autoescaped via Jinja's ``autoescape=True``
so a hostile free-text value (e.g. a crafted director string) cannot
inject HTML/JS into the rendered email.  The Python helpers in this
module never build raw HTML strings — all interpolation goes through
the template.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from mediaman.services.arr._types import RadarrMovie
from mediaman.services.downloads.download_format import extract_poster_url

if TYPE_CHECKING:
    from jinja2 import Template

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-cached Jinja environment.
#
# ``check_download_notifications`` is called once per library sync. Building
# a fresh ``Environment`` (filesystem walk + template compilation) every tick
# was wasted work, so we cache one env per process and reuse it for every
# call. The download-ready template lives next to the newsletter templates
# under ``mediaman/web/templates`` and never changes at runtime.
#
# rationale: _NOTIFICATION_LOCK guards lazy initialisation of the two globals
# below.  The notification path runs inside the APScheduler thread pool; without
# a lock two concurrent ticks can both observe ``_NOTIFICATION_ENV is None``
# and race to construct the Jinja environment (TOCTOU).
# ---------------------------------------------------------------------------
_NOTIFICATION_ENV = None
_NOTIFICATION_TEMPLATE = None
_NOTIFICATION_LOCK = threading.Lock()


def get_notification_template() -> Template:
    """Return the cached ``email/download_ready.html`` Jinja template.

    Built lazily on first use rather than at import time so unit tests
    that never trigger this code path don't pay the Jinja import cost.
    Thread-safe: _NOTIFICATION_LOCK prevents a TOCTOU race in the
    APScheduler thread pool.
    """
    global _NOTIFICATION_ENV, _NOTIFICATION_TEMPLATE
    if _NOTIFICATION_TEMPLATE is not None:
        return _NOTIFICATION_TEMPLATE
    with _NOTIFICATION_LOCK:
        # Double-checked: another thread may have initialised while we waited.
        if _NOTIFICATION_TEMPLATE is not None:
            return _NOTIFICATION_TEMPLATE
        from jinja2 import Environment, FileSystemLoader

        template_dir = Path(__file__).parent.parent.parent / "web" / "templates"
        _NOTIFICATION_ENV = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
        _NOTIFICATION_TEMPLATE = _NOTIFICATION_ENV.get_template("email/download_ready.html")
    return _NOTIFICATION_TEMPLATE


def gather_email_meta(
    row: sqlite3.Row,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, sqlite3.Row],
) -> dict[str, str]:
    """Assemble rich metadata dict for a notification email.

    Merges the suggestions-cache row (batch-fetched upstream) with a
    Radarr poster fallback.  Returns a flat dict of string values safe to
    pass directly to the Jinja template.
    """
    tmdb_id = row["tmdb_id"]
    service = row["service"]

    # Gather rich metadata for the email
    meta: dict[str, str] = {
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
            logger.warning("Failed to extract Radarr poster for notification", exc_info=True)

    return meta


def build_email_payload(
    row: sqlite3.Row,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, sqlite3.Row],
    template: Template,
) -> tuple[str, str]:
    """Build the email subject and rendered HTML body for a ready notification.

    Returns ``(subject, html)``.
    """
    title = row["title"]
    media_type = row["media_type"]

    meta = gather_email_meta(row, movie, suggestions_by_tmdb)
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
    return subject, html
