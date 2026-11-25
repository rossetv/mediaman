"""How a title's words are compared with what a person typed.

Both sides are folded alike: case, accents and apostrophes are dropped and "&" reads as "and".
Other punctuation is read both as a gap between words and as nothing, so "spider man" and
"spiderman" each find "Spider-Man".
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

# Hard cap on the user-supplied search term. Without this an attacker could
# submit a multi-megabyte string to be compared with every title.
# 200 chars is well above any realistic title length.
MAX_SEARCH_TERM_LEN = 200

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


# The spellings of the library's titles, which a search reads every one of.
_folds: dict[str, tuple[str, str]] = {}


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


class TitleSearch:
    """The titles of the library of *conn*, as compared with *query*."""

    def __init__(self, conn: sqlite3.Connection, query: str) -> None:
        query = query[:MAX_SEARCH_TERM_LEN]
        self._wanted = _spellings(query)
        # Where nothing but punctuation was typed, that is what to look for.
        self._typed = query.casefold().strip()
        items = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        if len(_folds) > 2 * items:  # each has its own title and its show's
            _folds.clear()

    def distance(self, title: str) -> int | None:
        """How far *title* is from the query: the same, the same but for a leading article,
        starting with it, with a word that does, or only containing it; None when it does
        not."""
        if not self._wanted[0]:
            return _distance(title.casefold().strip(), self._typed) if self._typed else None
        spellings = _folds.get(title)
        if spellings is None:
            spellings = _folds[title] = _spellings(title)
        pairs = zip(spellings, self._wanted, strict=True)
        found = [d for whole, want in pairs if (d := _distance(whole, want)) is not None]
        return min(found, default=None)


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
