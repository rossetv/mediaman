"""Download-queue sub-functions: NZBGet parsing and unmatched-NZB accumulation.

WHAT: Stateless helpers used by the orchestrator in ``__init__.py``:

- :func:`parse_nzb_queue`       — normalises raw NZBGet entries for matching.
- :func:`build_arr_items`       — matches Arr cards to NZBGet entries and builds items.
- :func:`add_unmatched_nzb_items` — appends manual NZBGet grabs with no Arr card.

WHY: Keeping these helpers here reduces ``__init__.py`` line count while leaving all
     monkeypatched names (``fetch_arr_queue``, ``build_nzbget_from_db``,
     ``maybe_trigger_search``, ``record_verified_completions``) in the package root
     where the test suite patches them.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import cast

from mediaman.services.arr.fetcher._base import ArrCard
from mediaman.services.downloads.download_format._types import DownloadItem
from mediaman.services.downloads.download_queue.classify import (
    arr_base_urls as _arr_base_urls,
)
from mediaman.services.downloads.download_queue.classify import (
    build_arr_link as _build_arr_link,
)
from mediaman.services.downloads.download_queue.classify import (
    build_search_hint as _build_search_hint,
)
from mediaman.services.downloads.download_queue.items import (
    build_matched_item as _build_matched_item,
)
from mediaman.services.downloads.download_queue.items import (
    build_unmatched_arr_item as _build_unmatched_arr_item_fn,
)
from mediaman.services.downloads.download_queue.items import (
    nzb_matches_arr,
)

logger = logging.getLogger(__name__)


def get_arr_base_urls(conn: sqlite3.Connection, secret_key: str) -> dict[str, str]:
    """Return the Radarr/Sonarr base URLs used for deep-link building."""
    return _arr_base_urls(conn, secret_key)


def _build_unmatched_arr_item_bound(
    arr: ArrCard,
    arr_base_urls_map: dict[str, str],
) -> DownloadItem:
    """Bind deep-link helpers and build an unmatched-arr download card."""
    return _build_unmatched_arr_item_fn(
        arr,
        arr_base_urls_map,
        _build_search_hint,
        cast(type, _build_arr_link),
    )


def parse_nzb_queue(
    nzb_queue: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Parse raw NZBGet queue entries into normalised dicts for matching.

    Each entry carries a ``_matched`` flag (initially False) so the
    arr-matching phase can claim entries without modifying the source list.
    """
    from mediaman.services.downloads.download_format import looks_like_series_nzb, parse_clean_title

    nzb_parsed: list[dict[str, object]] = []
    for nzb in nzb_queue:
        raw_name = nzb.get("NZBName", "")
        nzb_name = raw_name if isinstance(raw_name, str) else ""
        clean = parse_clean_title(nzb_name)
        raw_file_mb = nzb.get("FileSizeMB", 0)
        raw_remain_mb = nzb.get("RemainingSizeMB", 0)
        file_mb = raw_file_mb if isinstance(raw_file_mb, int | float) else 0
        remain_mb = raw_remain_mb if isinstance(raw_remain_mb, int | float) else 0
        done_mb = file_mb - remain_mb
        pct = round(done_mb / file_mb * 100) if file_mb > 0 else 0
        nzb_parsed.append(
            {
                "raw_status": nzb.get("Status", ""),
                "dl_id": nzb_name,
                "title": clean,
                "progress": pct,
                "file_mb": file_mb,
                "remain_mb": remain_mb,
                "done_mb": done_mb,
                "poster_url": "",
                "kind": "movie",
                "looks_like_series": looks_like_series_nzb(nzb_name),
                "_matched": False,
            }
        )
    return nzb_parsed


def build_arr_items(
    conn: sqlite3.Connection,
    arr_items: list[ArrCard],
    nzb_parsed: list[dict[str, object]],
    arr_base_urls_map: dict[str, str],
    download_rate: int,
    secret_key: str,
    trigger_search: Callable[..., None],
) -> tuple[list[DownloadItem], list[DownloadItem]]:
    """Match arr cards to NZBGet entries and build simplified queue items.

    Returns ``(items, upcoming_items)``.  NZBGet entries that match an arr
    card are marked ``_matched=True`` in place so the caller can identify
    unmatched (manual) NZBGet entries afterwards.

    ``trigger_search`` is passed in from the caller (``__init__.py``) so it
    resolves to the module-level name there — which means monkeypatch.setattr
    on ``mediaman.services.downloads.download_queue.maybe_trigger_search``
    correctly intercepts calls even though the matching logic lives here.
    """
    from mediaman.services.downloads.download_format import (
        build_item,
        format_eta,
        map_state,
        normalise_for_match,
    )

    items: list[DownloadItem] = []
    upcoming_items: list[DownloadItem] = []

    for arr in arr_items:
        if arr.get("is_upcoming"):
            upcoming_items.append(
                build_item(
                    dl_id=arr.get("dl_id", ""),
                    title=arr.get("title", "Unknown"),
                    media_type="series" if arr.get("kind") == "series" else "movie",
                    poster_url=arr.get("poster_url", ""),
                    state="upcoming",
                    progress=0,
                    eta="",
                    size_done="",
                    size_total="",
                    release_label=arr.get("release_label", ""),
                    arr_id=arr.get("arr_id") or 0,
                    kind=arr.get("kind") or "",
                )
            )
            continue

        arr_title_norm = normalise_for_match(arr.get("title") or "")
        release_name_norms = [
            n for n in (normalise_for_match(rn) for rn in (arr.get("release_names") or [])) if n
        ]
        arr_candidates = [c for c in [arr_title_norm, *release_name_norms] if c]
        arr_is_series = arr.get("kind") == "series"
        matched_nzb = None

        if arr_candidates:
            best_remain = -1.0
            for nzb in nzb_parsed:
                if nzb["_matched"]:
                    continue
                if not arr_is_series and nzb.get("looks_like_series"):
                    continue
                title_val = nzb.get("title") or ""
                nzb_t_norm = normalise_for_match(title_val if isinstance(title_val, str) else "")
                if not nzb_t_norm:
                    continue
                if nzb_matches_arr(nzb_t_norm, arr_candidates):
                    raw_remain = nzb.get("remain_mb", 0) or 0
                    remain = float(raw_remain) if isinstance(raw_remain, int | float) else 0.0
                    if remain > best_remain:
                        best_remain = remain
                        matched_nzb = nzb

        if matched_nzb and not matched_nzb["_matched"]:
            matched_nzb["_matched"] = True
            if arr_is_series and arr_candidates:
                for nzb in nzb_parsed:
                    if nzb["_matched"]:
                        continue
                    title_val2 = nzb.get("title") or ""
                    nzb_t_norm = normalise_for_match(
                        title_val2 if isinstance(title_val2, str) else ""
                    )
                    if nzb_t_norm and nzb_matches_arr(nzb_t_norm, arr_candidates):
                        nzb["_matched"] = True
            raw_status = matched_nzb["raw_status"]
            state = map_state(
                raw_status if isinstance(raw_status, str) else None, has_nzbget_match=True
            )
            raw_remain_mb = matched_nzb["remain_mb"]
            remain_mb_val = float(raw_remain_mb) if isinstance(raw_remain_mb, int | float) else 0.0
            eta = format_eta(remain_mb_val, download_rate)
            if state == "almost_ready":
                eta = "Post-processing…"

            items.append(_build_matched_item(arr, matched_nzb, state, eta, download_rate))
            trigger_search(conn, cast(dict, arr), matched_nzb=True, secret_key=secret_key)
        else:
            items.append(_build_unmatched_arr_item_bound(arr, arr_base_urls_map))
            trigger_search(conn, cast(dict, arr), matched_nzb=False, secret_key=secret_key)

    return items, upcoming_items


def add_unmatched_nzb_items(
    items: list[DownloadItem],
    nzb_parsed: list[dict[str, object]],
    download_rate: int,
) -> None:
    """Append unmatched NZBGet entries (manual grabs with no arr card) to *items* in place."""
    from mediaman.core.format import format_bytes
    from mediaman.services.downloads.download_format import build_item, format_eta, map_state

    for nzb in nzb_parsed:
        if nzb["_matched"]:
            continue
        raw_status = nzb["raw_status"]
        state = map_state(
            raw_status if isinstance(raw_status, str) else None, has_nzbget_match=True
        )
        raw_remain = nzb["remain_mb"]
        remain_mb_val = float(raw_remain) if isinstance(raw_remain, int | float) else 0.0
        eta = format_eta(remain_mb_val, download_rate)
        if state == "almost_ready":
            eta = "Post-processing…"
        media_type = "series" if nzb.get("looks_like_series") else "movie"
        dl_id_val = nzb["dl_id"]
        title_val = nzb["title"]
        progress_val = nzb["progress"]
        done_mb_val = nzb["done_mb"]
        file_mb_val = nzb["file_mb"]
        items.append(
            build_item(
                dl_id=dl_id_val if isinstance(dl_id_val, str) else "",
                title=title_val if isinstance(title_val, str) else "",
                media_type=media_type,
                poster_url="",
                state=state,
                progress=progress_val if isinstance(progress_val, int) else 0,
                eta=eta,
                size_done=format_bytes(
                    int(done_mb_val * 1024 * 1024) if isinstance(done_mb_val, int | float) else 0
                ),
                size_total=format_bytes(
                    int(file_mb_val * 1024 * 1024) if isinstance(file_mb_val, int | float) else 0
                ),
                arr_id=0,
                kind=media_type,
            )
        )
