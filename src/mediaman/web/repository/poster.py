"""Repository functions for poster-route reads.

The poster route is unusual in that it touches two table-groups —
``media_items`` (Arr poster fallback for a Plex rating key) and
``settings`` (the Plex URL + encrypted Plex token).  Both reads are
small and tightly coupled to the route's I/O path, so they live
together here rather than being split across ``library_query`` and a
settings repository.

The Plex-token read returns the *ciphertext* and ``encrypted`` flag;
decryption lives in the caller so the per-key AAD stays close to the
SSRF / URL-sanitising code that owns the rest of the credential flow.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PosterArrIds:
    """The Arr-side identifiers for a media_items row keyed by Plex rating key."""

    title: str
    media_type: str
    radarr_id: int | None
    sonarr_id: int | None


@dataclass(frozen=True)
class StoredPlexCredentials:
    """The raw settings rows backing the Plex URL + token.

    ``token_ciphertext`` is the on-disk value, which may be plaintext
    (``encrypted=False``) or AES-GCM ciphertext (``encrypted=True``).
    The caller decrypts via ``mediaman.crypto.decrypt_value`` with the
    appropriate per-key AAD.
    """

    url: str | None
    token_ciphertext: str | None
    token_encrypted: bool


def fetch_arr_ids(conn: sqlite3.Connection, rating_key: str) -> PosterArrIds | None:
    """Return the media_items row keyed by ``rating_key`` for poster fallback."""
    row = conn.execute(
        "SELECT title, media_type, radarr_id, sonarr_id FROM media_items WHERE id = ?",
        (rating_key,),
    ).fetchone()
    if row is None:
        return None
    return PosterArrIds(
        title=row["title"],
        media_type=row["media_type"] or "movie",
        radarr_id=row["radarr_id"],
        sonarr_id=row["sonarr_id"],
    )


def fetch_plex_credentials(conn: sqlite3.Connection) -> StoredPlexCredentials | None:
    """Return the stored Plex URL + token, or None when either is missing."""
    url_row = conn.execute("SELECT value FROM settings WHERE key='plex_url'").fetchone()
    token_row = conn.execute(
        "SELECT value, encrypted FROM settings WHERE key='plex_token'"
    ).fetchone()
    if url_row is None or token_row is None:
        return None
    return StoredPlexCredentials(
        url=url_row["value"],
        token_ciphertext=token_row["value"],
        token_encrypted=bool(token_row["encrypted"]),
    )


__all__ = [
    "PosterArrIds",
    "StoredPlexCredentials",
    "fetch_arr_ids",
    "fetch_plex_credentials",
]
