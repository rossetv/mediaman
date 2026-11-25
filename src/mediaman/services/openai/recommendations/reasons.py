"""Which reasons for a pick may be shown: checks of the model's wording."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from mediaman.services.openai.client import DOMAIN_RE

# Longest reason shown to people, and what none may hold: an address, an id or source in
# brackets, markdown.
_REASON_MAX_LEN = 200
_REASON_REFUSED = re.compile(
    rf"https?://|www\.|{DOMAIN_RE.pattern}|@"
    r"|[\[\]【】†]|\(\s*#?\d{1,3}(?:\s*,\s*#?\d{1,3})*\s*\)"
    r"|[*`]|__|^\s*(?:#|>|- )",
    re.IGNORECASE,
)
# Second-person wording, ids, and praise or talk of popularity that says nothing of the title.
_EMPTY_WORDS = re.compile(
    r"\b(?:you|your|yours|yourself|because|[HC]\d+"
    r"|must[- ](?:see|watch)|gripping|mind[- ]bending|edge[- ]of[- ](?:the|your)[- ]seat|buzz"
    r"|chatter|acclaimed|perfect for|everyone is talking|expected to|nostalgia[- ]driven"
    r"|hidden gem|thought[- ]provoking|heart[- ]?warming|feel[- ]good|unforgettable|masterpiece"
    r"|captivating|compelling|tour de force|genre fans|something for everyone|sure to"
    r"|roller ?coaster|smash hit|fan favou?rite|similar (?:vibe|themes?|tone)s?)\b",
    re.IGNORECASE,
)
# What would word a pick everyone sees as one made for a person or this household.
_PERSONAL_WORDS = re.compile(
    r"\b(?:recent (?:viewing|watches)|(?:viewing|watch) history|this (?:household|viewer|person)"
    r"|already (?:watched|seen|enjoyed|loved)|(?:been|recently) watch(?:ing|ed)"
    r"|personal(?:ly)? (?:pick|picked|chosen|recommended)|we|us|our)\b",
    re.IGNORECASE,
)
# A reason with this many words in a row of TMDB's description retells it.
_COPIED_WORDS = 8
_HYPHEN_RE = re.compile(r"(?<=\w)[-‐‑](\w)")
_APOSTROPHES = str.maketrans(dict.fromkeys("’ʼ`", "'"))
_NUMBER_WORDS = {
    word: str(n)
    for words in (
        (
            "Two",
            "Three",
            "Four",
            "Five",
            "Six",
            "Seven",
            "Eight",
            "Nine",
            "Ten",
            "Eleven",
            "Twelve",
        ),
        ("II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"),
    )
    for n, word in enumerate(words, start=2)
}
_NUMBER_WORD_RE = re.compile(rf"\b(?:{'|'.join(_NUMBER_WORDS)})\b")
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?…][\"”')]?\s)\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?][\"”')]?(?=\s|$)")

#: How many reasons of one set may have each shape.
SHAPE_MAX = {"like": 1, "here": 3, "shares": 3, "turn": 4}


def checked_reason(
    reason: str,
    sent_titles: Iterable[str],
    *,
    own_titles: Iterable[str] = (),
    shared: bool = False,
    description: str | None = None,
) -> str:
    """*reason* as it may be shown, cut to whole sentences; empty when it holds an address, an
    id, markdown, second-person wording or an empty phrase, names one of *sent_titles* that is
    none of *own_titles* (the titles that led to the pick), copies *description*, or cannot be
    cut. A *shared* pick's may not sound made for one person either.
    """
    if _REASON_REFUSED.search(reason) or _copies(reason, description):
        return ""
    own = list(own_titles)
    # "You" can be the title that led to the pick.
    spellings = [_without(text, own) for text in _spellings(reason)]
    if _EMPTY_WORDS.search(spellings[0]) or (shared and _PERSONAL_WORDS.search(spellings[0])):
        return ""
    if any(_names(spellings, title) for title in sent_titles if title not in own):
        return ""
    if len(reason) <= _REASON_MAX_LEN:
        return reason
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(reason) if m.end() <= _REASON_MAX_LEN]
    return reason[: ends[-1]] if ends else ""


def is_tied(reason: str, own_titles: Iterable[str]) -> bool:
    """Whether *reason* names one of the titles that led to the pick."""
    spellings = _spellings(reason)
    return any(_without(text, [title]) != text for title in own_titles for text in spellings)


_LIKE_RE = re.compile(
    r"(?:much |just |rather )?(?:like|as (?:in|with|on)|similar to|in the (?:vein|spirit) of)\b",
    re.IGNORECASE,
)
_HERE_RE = re.compile(r"\bhere\b", re.IGNORECASE)
_TURN_RE = re.compile(r"[,;] (?:but|only|though|while|yet)\b", re.IGNORECASE)
_SHARES_RE = re.compile(r"(?:it |this )?(?:shares|has)\b", re.IGNORECASE)


def shape_of(reason: str) -> str | None:
    """The template *reason* is built on, if one of those a set soon repeats."""
    if _LIKE_RE.match(reason):
        return "like"
    if _HERE_RE.search(reason):
        return "here"
    if _SHARES_RE.match(reason):
        return "shares"
    return "turn" if _TURN_RE.search(reason) else None


def repeated_shapes(reasons: Iterable[str]) -> set[str]:
    """The shapes that more of *reasons* have than a set can take."""
    counts = Counter(shape for reason in reasons if (shape := shape_of(reason)))
    return {shape for shape, count in counts.items() if count > SHAPE_MAX[shape]}


def _without(text: str, titles: Sequence[str]) -> str:
    for title in titles:
        for spelled in dict.fromkeys(_spellings(title)):
            if spelled.strip():
                text = re.sub(rf"(?<!\w){re.escape(spelled)}(?!\w)", "X", text)
    return text


def _copies(reason: str, description: str | None) -> bool:
    words = re.findall(r"\w+", reason.casefold())
    source = f" {' '.join(re.findall(r'\w+', (description or '').casefold()))} "
    return any(
        f" {' '.join(words[n : n + _COPIED_WORDS])} " in source
        for n in range(len(words) - _COPIED_WORDS + 1)
    )


def _spellings(text: str) -> list[str]:
    """*text* with its hyphenated words as two and as one, so "Spider-Man" is "Spiderman" too;
    last each again with its numbers as digits, so "Rocky II" is "Rocky 2"."""
    text = text.translate(_APOSTROPHES)
    words = [_HYPHEN_RE.sub(r" \1", text), _HYPHEN_RE.sub(lambda m: m[1].lower(), text)]
    return words + [_NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m[0]], w) for w in words]


def _names(reason_spellings: Sequence[str], title: str) -> bool:
    """Whether a reason names *title*. A title of one word is also an ordinary word, so it
    names nothing where a sentence begins, and its number is not looked for in digits."""
    one_word = re.fullmatch(r"\w+", title) is not None
    spellings = _spellings(title)
    for spelled in dict.fromkeys(spellings[:2] if one_word else spellings):
        if not spelled.strip():
            continue
        for reason in reason_spellings[:2] if one_word else reason_spellings:
            at = reason.find(spelled)
            while at >= 0:
                before, after = reason[:at], reason[at + len(spelled) :]
                if not (re.search(r"\w$", before) or re.match(r"\w", after)) and not (
                    one_word and _SENTENCE_START_RE.search(before)
                ):
                    return True
                at = reason.find(spelled, at + 1)
    return False
