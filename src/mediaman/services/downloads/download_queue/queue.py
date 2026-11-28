"""Download-queue sub-functions: NZBGet parsing and unmatched-NZB accumulation.

Stateless helpers used by the orchestrator in __init__.py. They live here to keep
__init__.py small while the monkeypatched names (fetch_arr_queue_result, build_nzbget_from_db,
maybe_trigger_search, record_verified_completions) stay in the package root, where the
tests patch them.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Collection, Sequence

from mediaman.services.arr.fetcher._base import ArrCard
from mediaman.services.downloads.download_format._types import DownloadItem
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

# A word in the NZBGet category that says the download is TV: NZBGet's and Sonarr's own names.
_TV_CATEGORY_WORDS = frozenset({"tv", "series", "sonarr"})


def _build_unmatched_arr_item_bound(
    arr: ArrCard,
    arr_base_urls_map: dict[str, str],
) -> DownloadItem:
    return _build_unmatched_arr_item_fn(
        arr,
        arr_base_urls_map,
        _build_search_hint,
        _build_arr_link,
    )


def _eta(state: str, remain_mb: float, download_rate: int) -> tuple[str, int | None]:
    """The time left of a download in *state*, in words and in minutes: none while it waits its
    turn, since NZBGet's speed is that of another download."""
    from mediaman.core.format import time_left
    from mediaman.services.downloads.download_format import eta_minutes

    if state == "almost_ready":
        return "Post-processing…", None
    if state == "paused":
        return "", None
    minutes = None if state == "queued" else eta_minutes(remain_mb, download_rate)
    return ("" if minutes is None else time_left(minutes)), minutes


def _download_ids(nzb: dict[str, object]) -> frozenset[str]:
    """The ids Radarr and Sonarr may know an NZBGet download by: the "drone" parameter they
    give it as they add it, and NZBGet's own NZBID."""
    ids = {str(nzb_id)} if isinstance(nzb_id := nzb.get("NZBID"), int) else set()
    params = nzb.get("Parameters")
    for param in params if isinstance(params, list) else []:
        if isinstance(param, dict) and param.get("Name") == "drone" and param.get("Value"):
            ids.add(str(param["Value"]))
    return frozenset(i.lower() for i in ids)


def parse_nzb_queue(
    nzb_queue: list[dict[str, object]], *, queue_paused: bool = False
) -> list[dict[str, object]]:
    """Parse raw NZBGet entries into normalised dicts with a _matched flag (initially
    False), so matching can claim entries without modifying the source list.

    While NZBGet's whole queue is *queue_paused*, nothing of it comes in: each that would is
    paused too, but by NZBGet's own pause, which no one download's resume lifts.
    """
    from mediaman.services.downloads.download_format import (
        looks_like_series_nzb,
        normalise_for_match,
        parse_clean_title,
    )

    nzb_parsed: list[dict[str, object]] = []
    for nzb in nzb_queue:
        raw_name = nzb.get("NZBName", "")
        nzb_name = raw_name if isinstance(raw_name, str) else ""
        clean = parse_clean_title(nzb_name)
        looks_like_series = looks_like_series_nzb(nzb_name)
        raw_category = nzb.get("Category", "")
        category = normalise_for_match(raw_category if isinstance(raw_category, str) else "")
        is_tv = looks_like_series or not _TV_CATEGORY_WORDS.isdisjoint(category.split())
        raw_file_mb = nzb.get("FileSizeMB", 0)
        raw_remain_mb = nzb.get("RemainingSizeMB", 0)
        file_mb = raw_file_mb if isinstance(raw_file_mb, int | float) else 0
        # Below 0 while a download fetches more than its nominal size.
        remain_mb = max(0, raw_remain_mb) if isinstance(raw_remain_mb, int | float) else 0
        done_mb = file_mb - remain_mb
        pct = max(0, round(done_mb / file_mb * 100)) if file_mb > 0 else 0
        raw_status = nzb.get("Status", "")
        status = raw_status if isinstance(raw_status, str) else ""
        paused_alone = status.upper() == "PAUSED" and not queue_paused
        if queue_paused and status.upper() in ("QUEUED", "DOWNLOADING", "FETCHING"):
            status = "PAUSED"
        nzb_id = nzb.get("NZBID")
        nzb_parsed.append(
            {
                "raw_status": status,
                "queue_paused": queue_paused,
                "paused_id": nzb_id if paused_alone and isinstance(nzb_id, int) else None,
                "download_ids": _download_ids(nzb),
                "name_norm": normalise_for_match(nzb_name),
                "dl_id": nzb_name,
                "title": clean,
                "progress": pct,
                "file_mb": file_mb,
                "remain_mb": remain_mb,
                "done_mb": done_mb,
                "poster_url": "",
                "kind": "series" if is_tv else "movie",
                "looks_like_series": looks_like_series,
                "_matched": False,
            }
        )
    return nzb_parsed


def _card_download_ids(arr: ArrCard) -> frozenset[str]:
    """The download client's ids Radarr or Sonarr gave of *arr*'s downloads."""
    ids = [arr.get("download_id") or ""]
    ids += [ep.get("download_id") or "" for ep in arr.get("episodes") or []]
    return frozenset(i.lower() for i in ids if i)


def _is_of(
    nzb: dict[str, object],
    arr_candidates: list[str],
    card_ids: frozenset[str],
    normalise_for_match: Callable[[str], str],
) -> bool:
    """Whether *nzb* is a download of the card: by the ids Radarr or Sonarr gave of it where
    NZBGet holds one of those, else by its title or its release's name, whole and exact, so that
    "Alone" never takes a download of "Home Alone"."""
    ids = nzb.get("download_ids")
    if card_ids and isinstance(ids, frozenset) and ids & card_ids:
        return True
    title_val = nzb.get("title") or ""
    nzb_t_norm = normalise_for_match(title_val if isinstance(title_val, str) else "")
    name_norm = nzb.get("name_norm")
    names = [n for n in (nzb_t_norm, name_norm) if isinstance(n, str) and n]
    return any(nzb_matches_arr(name, arr_candidates) for name in names)


def _find_best_nzb_match(
    arr_candidates: list[str],
    arr_is_series: bool,
    nzb_parsed: list[dict[str, object]],
    normalise_for_match: Callable[[str], str],
    card_ids: frozenset[str] = frozenset(),
) -> dict[str, object] | None:
    """Return the unmatched NZB entry of the card with the most data left to download, so a
    partly downloaded pack beats an older completed one; None if none is of it. One NZBGet
    holds by an id the card gives wins over any found by name alone.
    """
    if card_ids:
        by_id = [
            nzb
            for nzb in nzb_parsed
            if not nzb["_matched"]
            and isinstance(ids := nzb.get("download_ids"), frozenset)
            and ids & card_ids
        ]
        if by_id:
            return max(by_id, key=_remain_of)
    best_remain = -1.0
    matched_nzb = None
    for nzb in nzb_parsed:
        if nzb["_matched"]:
            continue
        if not arr_is_series and nzb.get("looks_like_series"):
            continue
        if _is_of(nzb, arr_candidates, frozenset(), normalise_for_match):
            raw_remain = nzb.get("remain_mb", 0) or 0
            remain = float(raw_remain) if isinstance(raw_remain, int | float) else 0.0
            if remain > best_remain:
                best_remain = remain
                matched_nzb = nzb
    return matched_nzb


def _remain_of(nzb: dict[str, object]) -> float:
    raw = nzb.get("remain_mb", 0) or 0
    return float(raw) if isinstance(raw, int | float) else 0.0


def _mark_series_nzb_matches(
    arr_candidates: list[str],
    nzb_parsed: list[dict[str, object]],
    normalise_for_match: Callable[[str], str],
    card_ids: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Mark NZB entries matching *arr_candidates* as _matched=True, claiming sibling NZBs
    (e.g. episodes when a pack is the primary match) so they are not shown as unmatched;
    return those it claimed.
    """
    claimed: list[dict[str, object]] = []
    for nzb in nzb_parsed:
        if nzb["_matched"]:
            continue
        if _is_of(nzb, arr_candidates, card_ids, normalise_for_match):
            nzb["_matched"] = True
            claimed.append(nzb)
    return claimed


# The order in which one of a card's NZBGet downloads speaks for the card: one still bringing
# data in, else one being unpacked, else one waiting its turn.
_STATE_RANK = ("downloading", "almost_ready", "queued", "paused")


def _card_state(nzbs: Sequence[dict[str, object]], map_state: Callable[..., str]) -> str:
    """The state of a card whose download is *nzbs*: that of the one furthest along."""
    states = {
        map_state(s if isinstance(s := nzb["raw_status"], str) else None, has_nzbget_match=True)
        for nzb in nzbs
    }
    return next((rank for rank in _STATE_RANK if rank in states), next(iter(states)))


def _mark_paused(item: DownloadItem, nzbs: Sequence[dict[str, object]]) -> None:
    """Say of a paused *item* whether NZBGet's whole queue is paused, and else give the ids of
    its downloads paused one by one, which an admin may resume."""
    if item["state"] != "paused":
        return
    if any(nzb.get("queue_paused") for nzb in nzbs):
        from mediaman.services.downloads.download_format._types import QUEUE_PAUSED

        item["state_label"] = QUEUE_PAUSED
        return
    ids = [i for nzb in nzbs if isinstance(i := nzb.get("paused_id"), int)]
    if ids:
        item["paused_ids"] = ids


def _remain_mb(nzbs: Sequence[dict[str, object]]) -> float:
    return sum(float(r) for nzb in nzbs if isinstance(r := nzb["remain_mb"], int | float))


def build_arr_items(
    conn: sqlite3.Connection,
    arr_items: list[ArrCard],
    nzb_parsed: list[dict[str, object]],
    arr_base_urls_map: dict[str, str],
    download_rate: int,
    secret_key: str,
    trigger_search: Callable[..., None],
    remembered: Sequence[ArrCard] = (),
    reused: Collection[str] = (),
) -> tuple[list[DownloadItem], list[DownloadItem]]:
    """Match arr cards to NZBGet entries and build queue items, returning (items,
    upcoming_items). Matched NZB entries are marked _matched=True in place.

    *trigger_search* is passed in from __init__.py so it resolves to the module-level
    name there, letting a monkeypatch of download_queue.maybe_trigger_search intercept
    calls.

    *remembered* are cards a service that did not answer gave before: one is listed only
    while NZBGet still holds its download, which it gives its name and art.

    *reused* are the dl_ids of cards a service gave in its last answer, standing in for one it
    missed: they may be out of date, so none starts a search.
    """
    from mediaman.services.downloads.download_format import (
        build_item,
        map_state,
        normalise_for_match,
    )

    items: list[DownloadItem] = []
    upcoming_items: list[DownloadItem] = []

    waiting = [arr for arr in remembered if not arr.get("is_upcoming")]
    for arr, answered in [*((a, True) for a in arr_items), *((a, False) for a in waiting)]:
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
                    label=arr.get("label", ""),
                )
            )
            continue

        arr_title_norm = normalise_for_match(arr.get("title") or "")
        release_name_norms = [
            n for n in (normalise_for_match(rn) for rn in (arr.get("release_names") or [])) if n
        ]
        arr_candidates = [c for c in [arr_title_norm, *release_name_norms] if c]
        arr_is_series = arr.get("kind") == "series"

        card_ids = _card_download_ids(arr)

        matched_nzb = (
            _find_best_nzb_match(
                arr_candidates, arr_is_series, nzb_parsed, normalise_for_match, card_ids
            )
            if arr_candidates or card_ids
            else None
        )

        if matched_nzb and not matched_nzb["_matched"]:
            matched_nzb["_matched"] = True
            nzbs = [matched_nzb]
            if arr_is_series:
                nzbs += _mark_series_nzb_matches(
                    arr_candidates, nzb_parsed, normalise_for_match, card_ids
                )
            if _arr_needs_attention(arr):
                # What Radarr or Sonarr says is wrong outweighs how NZBGet holds it.
                items.append(_build_unmatched_arr_item_bound(arr, arr_base_urls_map))
            else:
                # NZBGet fetches one download at a time, so the card is done once all of its are.
                state = _card_state(nzbs, map_state)
                eta, minutes = _eta(state, _remain_mb(nzbs), download_rate)
                item = _build_matched_item(
                    arr, matched_nzb, state, eta, download_rate, minutes_left=minutes
                )
                _mark_paused(item, nzbs)
                items.append(item)
            trigger_search(conn, arr, matched_nzb=True, secret_key=secret_key)
        elif answered:
            items.append(_build_unmatched_arr_item_bound(arr, arr_base_urls_map))
            if arr.get("dl_id") not in reused:
                trigger_search(conn, arr, matched_nzb=False, secret_key=secret_key)

    return items, upcoming_items


def _arr_needs_attention(arr: ArrCard) -> bool:
    """Whether Radarr or Sonarr says *arr*'s download needs a person, or has stalled."""
    from mediaman.services.downloads.download_format import map_arr_status, map_queue_state

    if arr.get("kind") == "series":
        episodes = arr.get("episodes") or []
        return bool(episodes) and map_queue_state(episodes) == "needs_attention"
    state = map_arr_status(
        arr.get("status", ""),
        arr.get("tracked_state", ""),
        arr.get("tracked_status", ""),
        arr.get("attention", ""),
    )
    return state == "needs_attention"


def add_unmatched_nzb_items(
    items: list[DownloadItem],
    nzb_parsed: list[dict[str, object]],
    download_rate: int,
) -> None:
    from mediaman.core.format import format_bytes
    from mediaman.services.downloads.download_format import build_item, map_state

    for nzb in nzb_parsed:
        if nzb["_matched"]:
            continue
        raw_status = nzb["raw_status"]
        state = map_state(
            raw_status if isinstance(raw_status, str) else None, has_nzbget_match=True
        )
        raw_remain = nzb["remain_mb"]
        remain_mb_val = float(raw_remain) if isinstance(raw_remain, int | float) else 0.0
        eta, minutes = _eta(state, remain_mb_val, download_rate)
        media_type = "series" if nzb["kind"] == "series" else "movie"
        dl_id_val = nzb["dl_id"]
        title_val = nzb["title"]
        progress_val = nzb["progress"]
        done_mb_val = nzb["done_mb"]
        file_mb_val = nzb["file_mb"]
        item = build_item(
            dl_id=dl_id_val if isinstance(dl_id_val, str) else "",
            title=title_val if isinstance(title_val, str) else "",
            media_type=media_type,
            poster_url="",
            state=state,
            progress=progress_val if isinstance(progress_val, int) else 0,
            eta=eta,
            minutes_left=minutes,
            size_done=format_bytes(
                int(done_mb_val * 1024 * 1024) if isinstance(done_mb_val, int | float) else 0
            ),
            size_total=format_bytes(
                int(file_mb_val * 1024 * 1024) if isinstance(file_mb_val, int | float) else 0
            ),
            arr_id=0,
            kind=media_type,
        )
        _mark_paused(item, [nzb])
        items.append(item)
