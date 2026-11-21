"""Anime detection, kept standalone so callers need not import the Plex dependency tree."""

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

    True for an Anime genre tag, or an Animation tag plus a studio in _JP_STUDIOS. The
    studio check avoids false positives for Western animation that carries only the
    Animation tag.
    """
    genres = {g.tag.lower() for g in getattr(show, "genres", [])}
    if "anime" in genres:
        return True
    if "animation" not in genres:
        return False
    studio = (show.studio or "").lower()
    return studio in _JP_STUDIOS
