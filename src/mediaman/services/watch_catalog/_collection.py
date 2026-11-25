"""TMDB's collection a film is part of, through the disk cache as ``_explore`` keeps a
person."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient, dicts

from ._discover import _CARD_FIELDS, _DAY, _cached, _failure, _is_gone, details
from ._explore import _iso_date
from ._shape import tmdb_id_of

_wall = time.time


def film_collection(
    client: TmdbClient, tmdb_id: int, *, allow_fetch: Callable[[], bool] | None = None
) -> dict[str, Any] | None:
    """The ``name`` and the ``films`` of the collection film *tmdb_id* is part of: the fields
    of a card of each, in the order of release, those without a date last and any TMDB marks
    adult left out. None when it is part of none. Raises TmdbUnavailable and TmdbBusy;
    *allow_fetch* is asked as in ``search``."""
    fields = details(client, "movie", tmdb_id, allow_fetch=allow_fetch)
    if "collection_id" not in fields:
        # Held from before the collection was. Asked for this film alone: a new cache key
        # would lose every title's details at once.
        fields = details(client, "movie", tmdb_id, allow_fetch=allow_fetch, anew=True)
    collection_id = fields["collection_id"]
    if collection_id is None:
        return None

    def fetch() -> dict[str, Any]:
        try:
            payload = client.collection(collection_id)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            if _is_gone(exc):
                return {"gone": True, "at": _wall()}
            raise _failure(exc, "collection") from exc
        films = {
            film_id: {
                **{f: p[f] for f in _CARD_FIELDS if f in p},
                "media_type": "movie",
                "release_date": _iso_date(p.get("release_date")),
            }
            for p in dicts(payload["parts"])
            if not p.get("adult") and (film_id := tmdb_id_of(p)) is not None
        }
        return {
            "name": str(payload["name"]),
            "films": sorted(films.values(), key=lambda f: f["release_date"] or "9999-12-31"),
        }

    key = f"tmdb:collection:v1:{collection_id}"
    result: dict[str, Any] = _cached(client, key, 7 * _DAY, 90 * _DAY, fetch, allow_fetch)
    if result.get("gone") and _wall() - float(result.get("at", 0)) >= _DAY:
        result = _cached(client, key, 0, 0, fetch, allow_fetch)
    return None if result.get("gone") else result
