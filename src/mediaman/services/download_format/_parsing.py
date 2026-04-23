"""Parsing and text-normalisation helpers for the downloads page."""

from __future__ import annotations

import re
import unicodedata

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

    Steps applied in order:
    1. Strip Unicode ``Cf`` (format) characters — these are invisible
       zero-width joiners, left-to-right marks, etc. that can make two
       visually identical strings compare as unequal.
    2. Lowercase.
    3. Replace every run of non-alphanumeric characters with a single
       space and trim.

    Fixes punctuation drift between a Sonarr series title like
    ``"Married at First Sight (AU)"`` and the cleaned NZB filename
    ``"Married at First Sight AU"`` — both normalise to
    ``"married at first sight au"`` so the substring check in
    :mod:`mediaman.services.download_queue` stops orphaning episodes.
    """
    s = (title or "").lower()
    # Strip Unicode Cf (format) characters — invisible glue that can
    # silently break equality checks between titles with/without BOM,
    # zero-width joiners, or directional marks.
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    return _MATCH_NORMALISE.sub(" ", s).strip()


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
