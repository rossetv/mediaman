"""What TMDB says of a show's season and its episodes, through the disk cache as ``_explore``
keeps a person."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient, dicts_in

from ._discover import _DAY, _cached, _failure, _is_gone
from ._explore import _iso_date

_wall = time.time


def season(client: TmdbClient, tmdb_id: int, number: int) -> dict[str, Any] | None:
    """The ``overview`` and ``aired`` date of a season, and those of its ``episodes`` by their
    number, as text. None when TMDB has no such season. Raises TmdbUnavailable and TmdbBusy."""

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
                str(e.get("episode_number")): _about(e) for e in dicts_in(payload, "episodes")
            },
        }

    key = f"tmdb:season:v1:{tmdb_id}:{number}"
    result: dict[str, Any] = _cached(client, key, 7 * _DAY, 90 * _DAY, fetch)
    if result.get("gone") and _wall() - float(result.get("at", 0)) >= _DAY:
        result = _cached(client, key, 0, 0, fetch)
    return None if result.get("gone") else result


def _about(payload: Mapping[str, Any]) -> dict[str, Any]:
    overview = payload.get("overview")
    return {
        "overview": overview.strip() if isinstance(overview, str) else "",
        "aired": _iso_date(payload.get("air_date")),
    }
