"""OMDb ratings fetch — extracted from the /download flow.

Best-effort. Returns an empty dict when the key is missing, the
request fails, or OMDb has nothing useful. Never raises.
"""
from __future__ import annotations

import logging

import requests

from mediaman.crypto import decrypt_value

logger = logging.getLogger("mediaman")


def _get_key(conn, secret_key: str) -> str | None:
    row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='omdb_api_key'"
    ).fetchone()
    if not row or not row["value"]:
        return None
    value = row["value"]
    if row["encrypted"]:
        try:
            value = decrypt_value(value, secret_key, conn=conn)
        except Exception:
            return None
    return value


def fetch_ratings(
    title: str,
    year: int | None,
    media_type: str,
    conn,
    secret_key: str,
) -> dict[str, str]:
    """Return ratings from OMDb.

    Keys in the returned dict (any subset): ``imdb``, ``rt``, ``metascore``.
    Missing values are omitted. Never raises.
    """
    key = _get_key(conn, secret_key)
    if not key:
        return {}

    params = {
        "apikey": key,
        "t": title,
        "type": "movie" if media_type == "movie" else "series",
    }
    if year:
        params["y"] = year

    try:
        resp = requests.get("https://www.omdbapi.com/", params=params, timeout=5)
        if not resp.ok:
            return {}
        data = resp.json()
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("Response") != "True":
        return {}

    out: dict[str, str] = {}
    imdb = data.get("imdbRating")
    if imdb and imdb != "N/A":
        out["imdb"] = imdb
    meta = data.get("Metascore")
    if meta and meta != "N/A":
        out["metascore"] = meta
    for r in data.get("Ratings", []):
        if r.get("Source") == "Rotten Tomatoes":
            out["rt"] = r["Value"]
            break
    return out
