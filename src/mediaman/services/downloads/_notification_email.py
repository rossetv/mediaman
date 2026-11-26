"""Email rendering helpers for download-ready notifications.

Owns the "build the Jinja template, gather TMDB/Sonarr/Radarr metadata,
and render the email payload" concern, kept apart from
mediaman.services.downloads.notifications so the orchestration
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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from mediaman.crypto import sign_tmdb_image_url
from mediaman.services.arr._types import RadarrMovie
from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.media_meta.item_enrichment import safe_backdrop

if TYPE_CHECKING:
    from jinja2 import Template

logger = logging.getLogger(__name__)

# A mail is read, and read again, long after it is sent, with no session.
_MAIL_PICTURE_DAYS = 180


@dataclass(frozen=True, slots=True)
class SuggestionsMeta:
    """Metadata from the suggestions table for one TMDB entry, so callers do not depend on
    its column order.

    Numeric columns (year, runtime) are pre-converted to str for the template.
    """

    tmdb_id: int
    year: str
    runtime: str
    director: str
    description: str
    rating: str
    imdb_rating: str
    rt_rating: str
    poster_url: str
    backdrop_url: str = ""


# Module-cached Jinja template.
#
# ``check_download_notifications`` is called once per library sync. Building
# a fresh ``Environment`` (filesystem walk + template compilation) every tick
# was wasted work, so we cache one template per process and reuse it for every
# call. The download-ready template lives adjacent to this module under
# ``mediaman/services/downloads/templates/`` and never changes at runtime.
#
# rationale: _NOTIFICATION_LOCK guards lazy initialisation of _NOTIFICATION_TEMPLATE.
# The notification path runs inside the APScheduler thread pool; without
# a lock two concurrent ticks can both observe ``_NOTIFICATION_TEMPLATE is None``
# and race to construct the Jinja environment (TOCTOU).
# Note: the Template object holds a strong reference to its Environment — a
# separate module-level env var would be dead state, so only the template is kept.
_NOTIFICATION_TEMPLATE: Template | None = None
_NOTIFICATION_LOCK = threading.Lock()


def get_notification_template() -> Template:
    """Return the cached download_ready.html template, built lazily so tests that never
    reach this path skip the Jinja import cost. _NOTIFICATION_LOCK prevents a race in
    the scheduler thread pool.
    """
    global _NOTIFICATION_TEMPLATE
    if _NOTIFICATION_TEMPLATE is not None:
        return _NOTIFICATION_TEMPLATE
    with _NOTIFICATION_LOCK:
        # Double-checked: another thread may have initialised while we waited.
        if _NOTIFICATION_TEMPLATE is not None:
            return _NOTIFICATION_TEMPLATE
        from jinja2 import Environment, FileSystemLoader

        services = Path(__file__).parent.parent
        # The second folder holds what the two mails share.
        folders = [
            services / "downloads" / "templates",
            services / "mail" / "newsletter" / "templates",
        ]
        env = Environment(loader=FileSystemLoader(folders), autoescape=True)
        _NOTIFICATION_TEMPLATE = env.get_template("download_ready.html")
    return _NOTIFICATION_TEMPLATE


def gather_email_meta(
    row: ClaimedNotificationRow,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, SuggestionsMeta],
) -> dict[str, str]:
    """Merge the batch-fetched suggestions row with a Radarr poster fallback into a flat
    dict of strings for the template.
    """
    tmdb_id = row.tmdb_id
    service = row.service

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
        "backdrop_url": "",
    }

    # Recommendations cache lookup — pulled from the batch fetch
    # above so we don't do per-row queries.
    rec = suggestions_by_tmdb.get(int(tmdb_id)) if tmdb_id else None
    if rec:
        for k in meta:
            val = getattr(rec, k, "")
            if val:
                meta[k] = val

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


def _site(base_url: str) -> str:
    """The site address with no trailing slash, or "" when it is no usable one."""
    site = base_url.strip().rstrip("/")
    return site if site.lower().startswith(("http://", "https://")) else ""


def _own_picture(url: str, size: str, site: str, secret_key: str) -> str:
    """*url* as this site's signed address when it is a TMDB picture and the site address is
    known; else *url* unchanged.
    """
    if not site or not secret_key:
        return url
    expires_at = int(time.time()) + _MAIL_PICTURE_DAYS * 86400
    signed = sign_tmdb_image_url(url, size, secret_key, expires_at=expires_at)
    return f"{site}{signed}" if signed else url


def _watch_url(row: ClaimedNotificationRow, base_url: str) -> str:
    """The address that opens the title on Watch, or "" (no button) with no usable site
    address. A title with no TMDB id is searched for by name.
    """
    site = _site(base_url)
    if not site:
        return ""
    if not row.tmdb_id:
        return f"{site}/watch?q={quote(row.title, safe='')}"
    kind = "movie" if row.media_type == "movie" else "tv"
    return f"{site}/watch#title=tmdb-{kind}-{row.tmdb_id}"


def build_email_payload(
    row: ClaimedNotificationRow,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, SuggestionsMeta],
    template: Template,
    base_url: str = "",
    secret_key: str = "",
) -> tuple[str, str]:
    title = row.title
    media_type = row.media_type

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

    # The wide backdrop heads the email; a title with none shows its poster as a poster.
    # Upgrade small TMDB posters to w500 for better-looking emails.
    # With the site address known, TMDB's pictures come from this site, so TMDB never learns
    # who opens the mail; Radarr's own poster address is left as it is.
    site = _site(base_url)
    backdrop_src = _own_picture(
        safe_backdrop(meta["backdrop_url"]) or "", "w1280", site, secret_key
    )
    poster_src = meta["poster_url"]
    if poster_src:
        poster_src = poster_src.replace("/w300", "/w500").replace("/w200", "/w500")
        poster_src = _own_picture(poster_src, "w500", site, secret_key)

    # A season row names its season so two seasons of one show do not send the same text.
    season_label = f"Season {row.season_number}" if row.season_number is not None else ""
    if row.season_number == 0:
        season_label = "Specials"
    what = f"'{title}' {season_label.lower()}" if season_label else f"'{title}'"
    verb = "are" if row.season_number == 0 else "is"
    subject = f"{what} {verb} now available to watch"
    html = template.render(
        title=title,
        season_label=season_label,
        poster_src=poster_src,
        backdrop_src=backdrop_src,
        meta=meta_ctx,
        ratings=ratings_ctx,
        description=meta["description"],
        watch_url=_watch_url(row, base_url),
    )
    return subject, html
