"""Anime detection logic extracted from :mod:`mediaman.services.media_meta.plex`.

Kept as a standalone module so other parts of the codebase (e.g. scanner
utilities) can detect anime without importing the full Plex dependency tree.
"""

from __future__ import annotations

from typing import Any

# Known Japanese animation studios.  Checked against the lower-cased Plex
# ``studio`` field when the show has the ``Animation`` genre but not the
# explicit ``Anime`` genre tag.
_JP_STUDIOS: frozenset[str] = frozenset(
    {
        "a-1 pictures",
        "bones",
        "cloverworks",
        "david production",
        "doga kobo",
        "j.c.staff",
        "kyoto animation",
        "lerche",
        "madhouse",
        "mappa",
        "o.l.m.",
        "olm",
        "orange",
        "p.a. works",
        "pierrot",
        "production i.g",
        "science saru",
        "shaft",
        "silver link.",
        "studio deen",
        "sunrise",
        "tms entertainment",
        "toei animation",
        "trigger",
        "ufotable",
        "white fox",
        "wit studio",
        "remow",
        "the answerstudio",
        "ezóla",
        "g&g entertainment",
    }
)


def is_anime(show: Any) -> bool:
    """Return True if *show* (a plexapi show object) is likely anime.

    Detection rules applied in order:
    1. Explicit ``Anime`` genre tag → True.
    2. ``Animation`` genre + studio name in :data:`_JP_STUDIOS` → True.
    3. Everything else → False.

    This two-tier check avoids false positives for Western animation
    (SpongeBob, Pixar films, etc.) that carry only the ``Animation`` tag.
    """
    genres = {g.tag.lower() for g in getattr(show, "genres", [])}
    if "anime" in genres:
        return True
    if "animation" not in genres:
        return False
    studio = (show.studio or "").lower()
    return studio in _JP_STUDIOS
