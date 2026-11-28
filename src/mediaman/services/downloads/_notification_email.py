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
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from mediaman.crypto import mail_tmdb_image_url
from mediaman.services.arr._types import ArrImage, RadarrMovie, SonarrSeries
from mediaman.services.downloads._notification_claims import ClaimedNotificationRow
from mediaman.services.downloads.download_format import extract_poster_url
from mediaman.services.media_meta.item_enrichment import safe_backdrop

if TYPE_CHECKING:
    from jinja2 import Template

logger = logging.getLogger(__name__)

# A picture Sonarr names, which is TheTVDB's: put in the mail as it is, as only TMDB's are
# served from this site.
_TVDB_IMAGE_RE = re.compile(
    r"https://artworks\.thetvdb\.com/[A-Za-z0-9_./-]{1,256}\.(?:jpg|jpeg|png|webp)"
)


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


#: A mail waits this long for TMDB at most; a slow answer leaves its blocks out.
_TMDB_TIMEOUT_S = 3.0

_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


def _tmdb_image(proxied: object) -> str:
    """The image.tmdb.org address of a Watch image-proxy address ("/api/watch/img/tmdb/w500/x.jpg"),
    which a mail cannot load; "" for none."""
    if not isinstance(proxied, str) or not proxied:
        return ""
    *_, size, name = proxied.split("/")
    return f"{_TMDB_IMAGE_BASE}/{size}/{name}"


def _tmdb_meta(
    conn: sqlite3.Connection, secret_key: str, kind: str, tmdb_id: int
) -> dict[str, str]:
    """The title's details as Watch shows them, from its TMDB cache or TMDB, with the ratings
    held; {} when TMDB is not set up or gives no answer."""
    from mediaman.services import watch_catalog as catalog
    from mediaman.services.media_meta.tmdb import TmdbClient

    try:
        client = TmdbClient.from_db(conn, secret_key, timeout=_TMDB_TIMEOUT_S, retry=False)
        if client is None:
            return {}
        fields = catalog.details(client, kind, tmdb_id, brief=True)
        held = conn.execute(
            "SELECT imdb_rating, rt_rating FROM ratings_cache WHERE tmdb_id = ? AND media_type = ?",
            (tmdb_id, kind),
        ).fetchone()
    # rationale: the details only dress the mail; whatever TMDB or its cache fails with, the
    # mail still goes out with the blocks it has.
    except Exception:
        logger.warning("TMDB details for a ready mail failed (tmdb=%s)", tmdb_id, exc_info=True)
        return {}
    runtime = fields.get("runtime_min")
    return {
        "year": str(fields.get("year") or ""),
        "runtime": str(runtime) if isinstance(runtime, int) and runtime > 0 else "",
        "director": str(fields.get("director") or ""),
        "description": str(fields.get("overview") or ""),
        "rating": str(fields.get("tmdb_rating") or ""),
        "imdb_rating": str(held[0] or "") if held else "",
        "rt_rating": str(held[1] or "") if held else "",
        "poster_url": _tmdb_image(fields.get("poster_url")),
        "backdrop_url": _tmdb_image(fields.get("backdrop_url")),
    }


def _remote_image(images: object, cover_type: str) -> str:
    """The first *cover_type* remoteUrl of an *arr images list, without its query; "" for none."""
    found: Sequence[ArrImage] = images if isinstance(images, list) else []
    for img in found:
        if not isinstance(img, dict):
            continue
        url = img.get("remoteUrl")
        if img.get("coverType") == cover_type and isinstance(url, str) and url:
            return url.partition("?")[0]
    return ""


def _sonarr_meta(series: SonarrSeries) -> dict[str, str]:
    year = series.get("year")
    runtime = series.get("runtime")
    return {
        "year": str(year) if isinstance(year, int) and year > 0 else "",
        "runtime": str(runtime) if isinstance(runtime, int) and runtime > 0 else "",
        "description": str(series.get("overview") or ""),
        "poster_url": _remote_image(series.get("images"), "poster"),
        "backdrop_url": _remote_image(series.get("images"), "fanart"),
    }


def gather_email_meta(
    row: ClaimedNotificationRow,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, SuggestionsMeta],
    *,
    conn: sqlite3.Connection | None = None,
    secret_key: str = "",
    series: SonarrSeries | None = None,
) -> dict[str, str]:
    """A flat dict of strings for the template: the batch-fetched suggestions row, else the
    title's TMDB details (asked once, with *conn*) and Sonarr's *series* for what they lack;
    then Radarr's poster when there is still none.
    """
    tmdb_id = row.tmdb_id or (series.get("tmdbId") if series else None)
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
    else:
        if tmdb_id and conn is not None:
            kind = "movie" if row.media_type == "movie" else "tv"
            meta.update(_tmdb_meta(conn, secret_key, kind, int(tmdb_id)))
        if service == "sonarr" and series:
            for k, val in _sonarr_meta(series).items():
                meta[k] = meta[k] or val

    # Fall back to Radarr for poster if missing
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
    """The site's address with no trailing slash, or "" when it is not a web address."""
    site = base_url.strip().rstrip("/")
    return site if site.lower().startswith(("http://", "https://")) else ""


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


def _mail_picture(image: str, size: str, secret_key: str, base_url: str) -> str:
    """The address a mail shows *image* at: TMDB's as mail_tmdb_image_url gives it, TheTVDB's
    (the pictures Sonarr names) as it is; "" for any other."""
    if _TVDB_IMAGE_RE.fullmatch(image):
        return image
    return mail_tmdb_image_url(image, size, secret_key, base_url) or ""


def build_email_payload(
    row: ClaimedNotificationRow,
    movie: RadarrMovie | None,
    suggestions_by_tmdb: dict[int, SuggestionsMeta],
    template: Template,
    base_url: str = "",
    secret_key: str = "",
    conn: sqlite3.Connection | None = None,
    series: SonarrSeries | None = None,
) -> tuple[str, str]:
    """The subject and HTML of *row*'s ready mail. Its pictures are TMDB's, else Sonarr's or
    Radarr's; a show's mail is text only when there is neither a TMDB key nor Sonarr's entry."""
    title = row.title
    media_type = row.media_type

    meta = gather_email_meta(
        row, movie, suggestions_by_tmdb, conn=conn, secret_key=secret_key, series=series
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

    # The wide backdrop heads the email; a title with none shows its poster as a poster.
    # Upgrade small TMDB posters to w500 for better-looking emails. TMDB's pictures are shown
    # from this site where its address is known; Radarr's host never learns who reads.
    backdrop = safe_backdrop(meta["backdrop_url"]) or meta["backdrop_url"]
    backdrop_src = _mail_picture(backdrop, "w1280", secret_key, base_url)
    poster = meta["poster_url"].replace("/w300", "/w500").replace("/w200", "/w500")
    poster_src = _mail_picture(poster, "w500", secret_key, base_url)

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
        site=_site(base_url),
    )
    return subject, html
