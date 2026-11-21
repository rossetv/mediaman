"""Shared redownload helpers: lookup matching and audit-ID selection.

Used by both the Radarr and Sonarr branches, so they live in a dependency-free module
to avoid an import cycle with .redownload. redownload re-exports both names so
tests can patch them there.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence

# Minimum title similarity accepted for a title+year fuzzy match.
_REDOWNLOAD_TITLE_SIMILARITY = 0.9


def _pick_lookup_match(
    lookup: Sequence[Mapping[str, object]],
    *,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
) -> tuple[Mapping[str, object] | None, str | None]:
    if not lookup:
        return None, "No lookup results"

    wanted_ids: dict[str, object] = {}
    if tmdb_id is not None:
        wanted_ids["tmdbId"] = tmdb_id
    if tvdb_id is not None:
        wanted_ids["tvdbId"] = tvdb_id
    if imdb_id:
        wanted_ids["imdbId"] = imdb_id

    if wanted_ids:
        hits = []
        for entry in lookup:
            for key, wanted in wanted_ids.items():
                got = entry.get(key)
                if got is None:
                    continue
                if str(got).strip().lower() == str(wanted).strip().lower():
                    hits.append(entry)
                    break
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "Ambiguous ID match"
        return None, "Supplied ID did not match any lookup result"

    if not title:
        return None, "No title for fuzzy match"

    target = title.strip().lower()
    scored: list[tuple[float, Mapping[str, object]]] = []
    for entry in lookup:
        cand_title = str(entry.get("title") or "").strip().lower()
        if not cand_title:
            continue
        ratio = difflib.SequenceMatcher(None, target, cand_title).ratio()
        scored.append((ratio, entry))
    if not scored:
        return None, "No titled lookup results"
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    if best_score < _REDOWNLOAD_TITLE_SIMILARITY:
        return None, "No confident title match"
    if year is None or best.get("year") != year:
        return None, "Year mismatch or missing"
    close = [
        entry
        for score, entry in scored
        if score >= _REDOWNLOAD_TITLE_SIMILARITY and entry.get("year") == year
    ]
    if len(close) > 1:
        return None, "Ambiguous title+year match"
    return best, None


def _redownload_audit_id(
    *,
    media_type: str,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
) -> str:
    """Pick a stable audit-log media_item_id for a redownload.

    Prefers tmdb:<id> (universal across both Arrs), then tvdb:<id>, then imdb:<id>. The
    prefix makes the column self-describing so the dashboard cannot match an opaque integer
    against unrelated UUIDs.
    """
    if tmdb_id is not None:
        return f"tmdb:{tmdb_id}"
    if tvdb_id is not None:
        return f"tvdb:{tvdb_id}"
    if imdb_id:
        return f"imdb:{imdb_id}"
    # Should never happen — caller guarantees at least one stable id is
    # present before we reach here.
    return f"redownload:{media_type}"
