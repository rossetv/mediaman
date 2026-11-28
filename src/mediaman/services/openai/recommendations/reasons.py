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
    r"\b(?:[HC]\d+"
    r"|must[- ](?:see|watch)|gripping|mind[- ]bending|edge[- ]of[- ](?:the|your)[- ]seat|buzz"
    r"|chatter|acclaimed|perfect for|everyone is talking|expected to|nostalgia[- ]driven"
    r"|hidden gem|thought[- ]provoking|heart[- ]?warming|feel[- ]good|unforgettable|masterpiece"
    r"|captivating|compelling|tour de force|genre fans|something for everyone|sure to"
    r"|roller ?coaster|smash hit|fan favou?rite|similar (?:vibe|themes?|tone)s?)\b",
    re.IGNORECASE,
)
# Speaking to the person: only a reason for them alone that names what led to the pick may.
_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|because)\b", re.IGNORECASE)
# What stands for a title that led to the pick once it is taken out of a reason: a character
# no title or word holds.
_MARK = "\ue000"
# "You loved ...": a claim about what the person played, whose object must be a title that
# led to the pick (each is _MARK by then). *love* marks the claims of having loved it.
_ADVERBS = r"(?:\s+(?:also|both|really|clearly|already|once|just|truly))*"
_CLAIM_RES = (
    re.compile(
        rf"\byou(?:'ve|'d|\s+have|\s+had)?{_ADVERBS}\s+(?:(?P<love>loved|adored|devoured)"
        r"|liked|enjoyed|watched|played|saw|seen|rated|finished|binged|rewatched|stayed with"
        r"|stuck with|sat through|went for|took to"
        r"|been\s+(?:watching|enjoying|playing|(?P<loving>loving)))\s+(?P<rest>[^.!?;]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:you're|you\s+are|as)\s+(?:a\s+)?(?:big\s+|huge\s+|real\s+)?fan\s+of\s+"
        r"(?P<rest>[^.!?;]*)",
        re.IGNORECASE,
    ),
)
# "Fans of ... like you": the titles between are claimed, and nothing else may be there.
_FANS_LIKE_YOU_RE = re.compile(r"\bfans?\s+of\s+(?P<rest>[^.!?;]*?)\s+like\s+you\b", re.IGNORECASE)
_CLAIMED_RE = re.compile(
    r"(?:(?:both|all of|every (?:season|episode) of|the whole of)\s+)?"
    rf"{_MARK}(?:\s*(?:,\s*and|,|and|&)\s*{_MARK})*"
)
# One more name after the claimed titles, where its clause ends: one more title claimed.
_ONE_MORE_RE = re.compile(
    r"\s*(?:,\s*and|,|and|&)\s*(?:the\s+)?[A-Z0-9][\w'’:-]*"
    r"(?:\s+(?:of|the|a|an|in|on|to|&|[A-Z0-9][\w'’:-]*))*\s*(?:,|$)"
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
    loved: Iterable[str] | None = None,
) -> str:
    """*reason* as it may be shown, cut to whole sentences; empty when it holds an address, an
    id, markdown or an empty phrase, names one of *sent_titles* that is none of *own_titles*
    (the titles that led to the pick), copies *description*, or cannot be cut. It may speak to
    the person ("you") only when it is for them alone and names one of *own_titles*, and may
    say they played only those, and loved only those of *loved* where that is given. A
    *shared* pick's may not sound made for one person.
    """
    if _REASON_REFUSED.search(reason) or _copies(reason, description) or _MARK in reason:
        return ""
    own = list(own_titles)
    # "You" can be the title that led to the pick.
    spellings = [_without(text, own) for text in _spellings(reason)]
    if _EMPTY_WORDS.search(spellings[0]) or (shared and _PERSONAL_WORDS.search(spellings[0])):
        return ""
    if _SECOND_PERSON.search(spellings[0]) and (shared or spellings[0] == _spellings(reason)[0]):
        return ""
    if not _claims_only(spellings[0], loved=False):
        return ""
    if loved is not None and not _claims_only(
        _without(_spellings(reason)[0], list(loved)), loved=True
    ):
        return ""
    if any(_names(spellings, title) for title in sent_titles if title not in own):
        return ""
    if len(reason) <= _REASON_MAX_LEN:
        return reason
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(reason) if m.end() <= _REASON_MAX_LEN]
    return reason[: ends[-1]] if ends else ""


#: What the card of a pick made for everyone says: nothing of it came from this person.
SHARED_WHY = "Chosen for everyone here, not from your own viewing."
_NO_TITLES_WHY = "From what you’ve been watching lately."


def pick_why(
    reason: str | None,
    because_of: Sequence[str],
    *,
    own: bool,
    disliked: Iterable[str] = (),
) -> str:
    """Why a pick suits the person it is shown to, never what happens in it: the *reason* the
    model wrote for it, checked as it was saved, else a line made of the titles of *because_of*
    they have not since rated down (*disliked*), else a plain one. Their own (*own*) pick's
    reason may not name a title they rated down; a shared one's, written for everyone, may not
    speak to one person, and with none its card says it was made for everyone."""
    reason = (reason or "").strip()
    if not own:
        personal = _SECOND_PERSON.search(reason) or _PERSONAL_WORDS.search(reason)
        return reason if reason and not personal else SHARED_WHY
    if reason and not names_any(reason, disliked):
        return reason
    down = {title.casefold() for title in disliked}
    led = [title for title in because_of if title.casefold() not in down]
    if not led:
        return _NO_TITLES_WHY
    named = led[0] if len(led) == 1 else f"{', '.join(led[:-1])} and {led[-1]}"
    return f"You watched {named}."


def names_any(reason: str, titles: Iterable[str]) -> bool:
    """Whether *reason* names one of *titles*."""
    spellings = _spellings(reason)
    return any(_names(spellings, title) for title in titles)


def _claims_only(text: str, *, loved: bool) -> bool:
    """Whether every claim in *text* of what the person played (of what they loved, with
    *loved*) names only titles marked as _MARK in it."""
    for claim in _CLAIM_RES:
        for m in claim.finditer(text):
            if loved and not (m.groupdict().get("love") or m.groupdict().get("loving")):
                continue
            named = _CLAIMED_RE.match(m["rest"])
            if named is None or _ONE_MORE_RE.match(m["rest"], named.end()):
                return False
    if loved:
        return True
    return all(_CLAIMED_RE.fullmatch(m["rest"]) for m in _FANS_LIKE_YOU_RE.finditer(text))


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
                text = re.sub(rf"(?<!\w){re.escape(spelled)}(?!\w)", _MARK, text)
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
