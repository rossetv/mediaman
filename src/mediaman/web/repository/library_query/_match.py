"""How a title's words are compared with what a person typed.

Both sides are folded alike: case, accents and apostrophes are dropped and "&" reads as "and".
Other punctuation is read both as a gap between words and as nothing, so "spider man" and
"spiderman" each find "Spider-Man".
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_ARTICLE = re.compile(r"^(?:the|an?)\s+(?=\S)")

# Dropped before accents are taken apart, which would make letters or gaps of some of them.
_DROPPED = str.maketrans(dict.fromkeys("'‘’ʼ′`´™℠", ""))
# Letters no accent can be taken off, as people type them.
_LETTERS = str.maketrans(
    {
        "æ": "ae",
        "œ": "oe",
        "ø": "o",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "ł": "l",
        "ħ": "h",
        "ı": "i",
        "&": " and ",
        "'": "",
    }
)


def without_article(text: str) -> str:
    """*text*, which is lower case, without a leading article."""
    return _ARTICLE.sub("", text)


@lru_cache(maxsize=20_000)
def _spellings(text: str) -> tuple[str, str]:
    """*text* folded, its punctuation read as gaps and then as nothing."""
    plain = "".join(
        c
        for c in unicodedata.normalize("NFKD", text.translate(_DROPPED))
        if not unicodedata.combining(c)
    )
    spaced: list[str] = []
    joined: list[str] = []
    for c in plain.casefold().translate(_LETTERS):
        if c.isspace():
            spaced.append(" ")
            joined.append(" ")
            continue
        category = unicodedata.category(c)
        if category == "Cf":
            continue
        if category[0] in "PSC":
            spaced.append(" ")
        else:
            spaced.append(c)
            joined.append(c)
    return " ".join("".join(spaced).split()), " ".join("".join(joined).split())


def title_distance(title: str, query: str) -> int | None:
    """How far *title* is from *query*: the same, the same but for a leading article, starting
    with it, with a word that does, or only containing it; None when it does not."""
    wanted = _spellings(query)
    if wanted[0]:
        pairs = zip(_spellings(title), wanted, strict=True)
        found = [d for whole, want in pairs if (d := _distance(whole, want)) is not None]
        return min(found, default=None)
    # Nothing but punctuation was typed, so that is what to look for.
    return _distance(title.casefold().strip(), query.casefold().strip())


def _distance(whole: str, wanted: str) -> int | None:
    if wanted not in whole:
        return None
    if wanted == whole:
        return 0
    name = without_article(whole)
    if wanted == name:
        return 1
    if whole.startswith(wanted) or name.startswith(wanted):
        return 2
    return 3 if f" {wanted}" in whole else 4
