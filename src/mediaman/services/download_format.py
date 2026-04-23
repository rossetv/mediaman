"""Pure format, parse, and classification helpers for the downloads page.

Every helper here is stateless — no DB, no HTTP — so they're trivially
testable and safe to reuse from other modules. Logic is lifted verbatim
from ``web/routes/downloads.py``; if a bug surfaces here, fix it here.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TypedDict

from mediaman.services.format import format_bytes, parse_iso_utc

logger = logging.getLogger("mediaman")


class DownloadItem(TypedDict):
    """A single item on the downloads page, as returned by build_item()."""

    id: str
    title: str
    media_type: str
    poster_url: str
    state: str
    progress: int
    eta: str
    size_done: str
    size_total: str
    episodes: list[dict] | None
    episode_summary: str
    release_label: str
    has_pack: bool
    search_count: int
    last_search_ts: float
    added_at: float
    search_hint: str
    arr_link: str
    arr_source: str


def extract_poster_url(images: list[dict] | None) -> str | None:
    """Return the first poster remoteUrl from an *arr images list, or None.

    Iterates the ``images`` list (as returned by Radarr/Sonarr API responses)
    and returns the ``remoteUrl`` of the first entry whose ``coverType`` is
    ``"poster"``. Returns ``None`` when no matching entry is found or when
    ``images`` is falsy.
    """
    for img in images or []:
        if img.get("coverType") == "poster" and img.get("remoteUrl"):
            return img["remoteUrl"]
    return None


def fmt_relative_time(ts: float, now: float) -> str:
    """Render a relative-time string like ``"12m ago"`` or ``"3d ago"``.

    Returns ``""`` when ``ts`` is non-positive (meaning "unknown"). Used
    for the "Last searched Xm ago" subline under the searching pill —
    kept identical shape to the JS helper in the download templates so
    server-rendered and poll-updated labels don't jitter.
    """
    if ts <= 0:
        return ""
    delta = int(now - ts)
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


_SERIES_MARKER = re.compile(r"\bS\d{2}(?:E\d{1,3})?\b", flags=re.IGNORECASE)


def looks_like_series_nzb(nzb_name: str) -> bool:
    """Return True when an NZB filename carries a SxxExx / Sxx marker.

    Used to stop a movie-kind arr item from greedily claiming a TV episode
    NZB via the loose substring title match (e.g. Radarr movie "The Great
    Escape" would otherwise steal NZBs for the "The Great" TV series).
    """
    return bool(_SERIES_MARKER.search(nzb_name or ""))


def parse_clean_title(nzb_name: str) -> str:
    """Extract a clean title from an NZB filename.

    Strips recognised technical tokens (year, resolution, codec, source,
    audio, episode markers) from both ends and keeps everything else as the
    title.  Year-prefixed names like ``"2021.Dune.1080p.x264"`` correctly
    return ``"Dune"`` rather than the year alone.

    Strategy:
    1. Normalise separators (dots/underscores -> spaces).
    2. Split on the first recognised token from the *left*.
    3. If that split yields an empty prefix (token was the first word, i.e.
       a year-prefixed name), strip the leading token and re-split on the
       remaining string from the *left* again, continuing until a non-empty
       prefix is found or we run out of tokens.
    """
    _TOKEN_PAT = re.compile(
        r"\b(?:(?:19|20)\d{2}|2160p|1080p|720p|480p|4K|UHD|HDR|BDRip|BluRay|WEB[-]?DL|"
        r"WEBRip|WEB|HDTV|DVDRip|BRRip|Remux|AMZN|NF|DSNP|HMAX|x264|x265|h264|h265|"
        r"HEVC|AAC|DTS|TrueHD|Atmos|DDP|DD5|AC3|FLAC|S\d{2}E?\d{0,2})\b",
        flags=re.IGNORECASE,
    )

    name = nzb_name.replace(".", " ").replace("_", " ")

    # Walk left-to-right: find the position of the first token match, take
    # everything before it.  If that prefix is empty the name starts with a
    # token (e.g. "2021 Dune 1080p") -- skip past that token and repeat.
    remaining = name
    while True:
        m = _TOKEN_PAT.search(remaining)
        if m is None:
            # No tokens found at all -- the whole string is the title.
            title = remaining.strip().rstrip("- ")
            break
        prefix = remaining[: m.start()].strip().rstrip("- ")
        if prefix:
            title = prefix
            break
        # Leading token -- skip it and continue.
        remaining = remaining[m.end():].strip()

    return title


_MATCH_NORMALISE = re.compile(r"[^a-z0-9]+")


def normalise_for_match(title: str) -> str:
    """Canonicalise a title for fuzzy substring matching against NZB names.

    Lowercases, replaces every run of non-alphanumeric characters with a
    single space, and trims. Fixes punctuation drift between a Sonarr
    series title like ``"Married at First Sight (AU)"`` and the cleaned
    NZB filename ``"Married at First Sight AU"`` — both normalise to
    ``"married at first sight au"`` so the substring check in
    :mod:`mediaman.services.download_queue` stops orphaning episodes.
    """
    return _MATCH_NORMALISE.sub(" ", (title or "").lower()).strip()


def map_state(nzbget_status: str | None, has_nzbget_match: bool) -> str:
    """Map internal NZBGet/Arr state to a user-facing state string.

    Returns one of: "searching", "downloading", "almost_ready".
    """
    if not has_nzbget_match or nzbget_status is None:
        return "searching"
    upper = nzbget_status.upper()
    if "UNPACKING" in upper or "PP_" in upper:
        return "almost_ready"
    if "DOWNLOADING" in upper or "PAUSED" in upper:
        return "downloading"
    return "searching"


def map_arr_status(status: str, tracked_state: str = "") -> str:
    """Map a Radarr/Sonarr queue item to a user-facing state.

    Checks both the queue ``status`` field and ``trackedDownloadState``
    since different Radarr/Sonarr versions populate these differently.

    Returns one of: ``"downloading"``, ``"almost_ready"``, ``"searching"``.
    """
    lower = (status or "").lower()
    tracked = (tracked_state or "").lower()

    # Check trackedDownloadState first — more reliable
    if tracked in ("importing", "importpending", "imported"):
        return "almost_ready"
    if tracked == "downloading":
        return "downloading"

    # Fall back to queue status
    if lower == "downloading":
        return "downloading"
    if lower == "completed":
        return "almost_ready"

    # If the item is in the queue at all, it's at least downloading
    # (queued/paused/delay all mean it's been grabbed)
    if lower in ("queued", "paused", "delay"):
        return "downloading"

    return "searching"


def _fmt_release_date(dt: "datetime") -> str:
    """Format a datetime as '<d MMM yyyy>' e.g. '14 Jun 2099'."""
    return dt.strftime("%-d %b %Y")


def classify_movie_upcoming(movie: dict) -> tuple[bool, str]:
    """Classify a Radarr movie as upcoming and build its release label.

    Returns (is_upcoming, release_label). Label is "" when not upcoming.
    """
    if not movie.get("monitored"):
        return False, ""
    if movie.get("hasFile"):
        return False, ""
    if movie.get("isAvailable"):
        return False, ""

    now = datetime.now(timezone.utc)
    candidates = []
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        dt = parse_iso_utc(movie.get(key, ""))
        if dt and dt > now:
            candidates.append(dt)

    if candidates:
        earliest = min(candidates)
        return True, f"Releases {_fmt_release_date(earliest)}"
    return True, "Not yet released"


def classify_series_upcoming(
    series: dict, episodes: list[dict]
) -> tuple[bool, str]:
    """Classify a Sonarr series as upcoming and build its premiere label.

    Returns ``(is_upcoming, release_label)``. Label is ``""`` when not upcoming.

    ``episodes`` is the list of episodes for this series (may be empty).
    An empty list is treated as "no aired episodes".

    Episodes whose ``airDateUtc`` field is missing or cannot be parsed are
    counted and logged at DEBUG level, then placed in an "unknown airdate"
    bucket.  They do not affect the classification but are not silently
    dropped -- the log entry shows the count so operators can investigate.
    """
    if not series.get("monitored"):
        return False, ""
    stats = series.get("statistics") or {}
    if stats.get("episodeFileCount", 0) > 0:
        return False, ""

    now = datetime.now(timezone.utc)
    status = (series.get("status") or "").lower()

    has_aired = False
    future_airs: list[datetime] = []
    unknown_count = 0

    for e in episodes:
        raw = e.get("airDateUtc", "")
        if not raw:
            unknown_count += 1
            continue
        dt = parse_iso_utc(raw)
        if dt is None:
            unknown_count += 1
            continue
        if dt < now:
            has_aired = True
        else:
            future_airs.append(dt)

    if unknown_count:
        logger.debug(
            "classify_series_upcoming: series=%r unknown_airdate_count=%d -- "
            "episodes with unparseable airDateUtc are in the unknown bucket",
            series.get("title", ""),
            unknown_count,
        )

    # Only classify as upcoming when we have a real signal:
    # - Sonarr explicitly says "upcoming", OR
    # - We can see future-dated episodes and no past-aired ones.
    if status == "upcoming":
        if future_airs:
            return True, f"Premieres {_fmt_release_date(min(future_airs))}"
        return True, "Not yet aired"

    if future_airs and not has_aired:
        return True, f"Premieres {_fmt_release_date(min(future_airs))}"

    return False, ""


def build_item(
    dl_id: str,
    title: str,
    media_type: str,
    poster_url: str,
    state: str,
    progress: int,
    eta: str,
    size_done: str,
    size_total: str,
    episodes: list[dict] | None = None,
    episode_summary: str = "",
    release_label: str = "",
    has_pack: bool = False,
    search_count: int = 0,
    last_search_ts: float = 0.0,
    added_at: float = 0.0,
    search_hint: str = "",
    arr_link: str = "",
    arr_source: str = "",
) -> DownloadItem:
    """Build a simplified download item for the API response.

    ``search_count`` / ``last_search_ts`` are populated only for items in
    the ``searching`` state and power the "Last searched Xm ago" subline
    in the UI. ``added_at`` is used as a fallback when mediaman hasn't
    fired a search yet (first 5 min, or across a restart).

    ``arr_link`` is the deep-link URL into Radarr/Sonarr for the item,
    and ``arr_source`` is ``"Radarr"`` or ``"Sonarr"`` — used to label
    the deep-link button.
    """
    return {
        "id": dl_id,
        "title": title,
        "media_type": media_type,
        "poster_url": poster_url,
        "state": state,
        "progress": progress,
        "eta": eta,
        "size_done": size_done,
        "size_total": size_total,
        "episodes": episodes,
        "episode_summary": episode_summary,
        "release_label": release_label,
        "has_pack": has_pack,
        "search_count": search_count,
        "last_search_ts": last_search_ts,
        "added_at": added_at,
        "search_hint": search_hint,
        "arr_link": arr_link,
        "arr_source": arr_source,
    }


def select_hero(items: list[dict]) -> tuple[dict | None, list[dict]]:
    """Pick the hero item from a list of download items.

    The actively downloading item with the highest progress becomes the hero.
    If nothing is downloading, the first item wins.
    Returns (hero, remaining_items).
    """
    if not items:
        return None, []
    if len(items) == 1:
        return items[0], []

    def sort_key(item):
        is_downloading = item["state"] == "downloading"
        return (not is_downloading, -item["progress"])

    ranked = sorted(items, key=sort_key)
    return ranked[0], ranked[1:]


def fmt_eta(remain_mb: float, download_rate: int) -> str:
    """Format ETA string from remaining MB and download rate (bytes/sec)."""
    if download_rate > 0 and remain_mb > 0:
        eta_sec = int(remain_mb * 1024 * 1024 / download_rate)
        if eta_sec >= 3600:
            return (
                f"~{eta_sec // 3600} hr"
                f" {(eta_sec % 3600) // 60:02d} min remaining"
            )
        if eta_sec >= 60:
            return f"~{eta_sec // 60} min remaining"
        return f"~{eta_sec} sec remaining"
    return ""


def map_episode_state(ep: dict) -> str:
    """Map a Sonarr episode entry to a simplified state.

    Returns one of: "ready", "downloading", "queued", "searching".

    NZBGet transfers one NZB at a time by default. Items sitting in the
    queue with partial or zero progress show up in Sonarr as ``paused``,
    ``queued`` or ``delay`` — calling those "downloading" overstates
    what's actually happening, so they get a distinct "queued" label.
    """
    progress = ep.get("progress", 0)
    sizeleft = ep.get("sizeleft", 0)
    size = ep.get("size", 0)
    status = (ep.get("status") or "").lower()

    if progress == 100 or (sizeleft == 0 and size > 0):
        return "ready"
    if status == "downloading":
        return "downloading"
    if status in ("paused", "queued", "delay", "warning") or progress > 0:
        return "queued"
    return "searching"


def fmt_episode_label(season: int | None, episode: int | None) -> str:
    """Format an episode label like ``"S01E02"`` or ``"S03"``.

    Returns ``""`` when *season* is ``None``. Omits the episode portion
    when *episode* is ``None`` (season-only pack entries).
    """
    if season is None:
        return ""
    label = f"S{season:02d}"
    if episode is not None:
        label += f"E{episode:02d}"
    return label


def build_episode_summary(episodes: list[dict]) -> str:
    """Build a human-readable summary like '2 of 8 episodes ready ...'."""
    total = len(episodes)
    ready = sum(1 for e in episodes if e["state"] == "ready")
    downloading = sum(1 for e in episodes if e["state"] == "downloading")
    queued = sum(1 for e in episodes if e["state"] == "queued")
    searching = sum(1 for e in episodes if e["state"] == "searching")

    parts = []
    if ready:
        parts.append(f"{ready} of {total} episodes ready")
    if downloading:
        parts.append(f"{downloading} downloading")
    if queued:
        parts.append(f"{queued} queued")
    if searching:
        parts.append(f"{searching} searching")
    return " \u00b7 ".join(parts)
