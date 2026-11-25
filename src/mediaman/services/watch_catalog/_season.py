"""What TMDB says of a show's season and its episodes, through the disk cache as ``_explore``
keeps a person."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient, dicts_in

from ._discover import _DAY, _cached, _failure, _is_gone, _peeked
from ._explore import _iso_date

_wall = time.time


def season(
    client: TmdbClient, tmdb_id: int, number: int, *, wait: bool = True
) -> dict[str, Any] | None:
    """The ``overview`` and ``aired`` date of a season, and those of its ``episodes`` by their
    number with their ``title``, as text. None when TMDB has no such season. Raises
    TmdbUnavailable and TmdbBusy, and disk_cache.Refused when TMDB has yet to answer and it is
    not to *wait*: TMDB is then asked in the background."""

    def fetch() -> dict[str, Any]:
        try:
            payload = client.season(tmdb_id, number)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            if _is_gone(exc):
                return {"gone": True, "at": _wall()}
            raise _failure(exc, "season") from exc
        # Only these fields are cached: TMDB's answer carries the crew of every episode.
        return {
            **_about(payload),
            "episodes": {
                str(e.get("episode_number")): {**_about(e), "title": _text(e.get("name"))}
                for e in dicts_in(payload, "episodes")
            },
        }

    # "v2": entries before it lack the episodes' titles.
    key = f"tmdb:season:v2:{tmdb_id}:{number}"
    result: dict[str, Any] | None
    if wait:
        result = _cached(client, key, 7 * _DAY, 90 * _DAY, fetch)
    else:
        result = _peeked(client, key, 7 * _DAY, 90 * _DAY, fetch)
    if result is None:
        raise disk_cache.Refused(key)
    if result.get("gone") and _wall() - float(result.get("at", 0)) >= _DAY:
        if not wait:
            raise disk_cache.Refused(key)
        result = _cached(client, key, 0, 0, fetch)
    return None if not result or result.get("gone") else result


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _about(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "overview": _text(payload.get("overview")),
        "aired": _iso_date(payload.get("air_date")),
    }
