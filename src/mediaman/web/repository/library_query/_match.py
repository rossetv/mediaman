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
        return _measure(spellings, self._wanted)


def title_distance(query: str, title: str) -> int | None:
    """As ``TitleSearch.distance``, of one title."""
    wanted = _spellings(query[:MAX_SEARCH_TERM_LEN])
    if not wanted[0]:
        return None
    return _measure(_spellings(title), wanted)


def loosened(query: str) -> str:
    """*query* as a search elsewhere is asked for it again: folded, without punctuation and
    with no plural."""
    return " ".join(_stem(w) for w in _spellings(query[:MAX_SEARCH_TERM_LEN])[0].split())


def _measure(spellings: tuple[str, str], wanted: tuple[str, str]) -> int | None:
    pairs = zip(spellings, wanted, strict=True)
    found = [d for whole, want in pairs if (d := _distance(whole, want)) is not None]
    if found:
        return min(found)
    return FORGIVEN if forgiven(wanted[0], spellings[0]) else None


# After every title that holds what was typed.
FORGIVEN = 5


def _stem(word: str) -> str:
    """*word* without the ending of a plural."""
    if len(word) < 4 or word.isdigit():
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "zes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _near(a: str, b: str, most: int) -> bool:
    """Whether *a* becomes *b* by at most *most* letters added, dropped, changed or swapped."""
    if abs(len(a) - len(b)) > most:
        return False
    before = list(range(len(b) + 1))
    row = before
    for i, ca in enumerate(a, 1):
        now = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            now[j] = min(now[j - 1] + 1, row[j] + 1, row[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                now[j] = min(now[j], before[j - 2] + 1)
        if min(now) > most:
            return False
        before, row = row, now
    return row[-1] <= most


def _word_fits(typed: str, word: str, last: bool) -> bool:
    if typed == word or (last and len(typed) >= 2 and word.startswith(typed)):
        return True
    a, b = _stem(typed), _stem(word)
    if a == b:
        return True
    # A typo is forgiven only in a word long enough that another word is not meant.
    if len(a) < 5 or typed.isdigit() or word.isdigit():
        return False
    return _near(a, b, 2 if len(a) >= 9 else 1)


def forgiven(typed: str, title: str) -> bool:
    """Whether every word of *typed* is a word of *title* but for a plural or a small typo,
    both folded as TitleSearch folds them; the last may be the start of one."""
    wanted = typed.split()
    words = title.split()
    if not wanted or not words:
        return False
    return all(
        any(_word_fits(w, word, i == len(wanted) - 1) for word in words)
        for i, w in enumerate(wanted)
    )


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
