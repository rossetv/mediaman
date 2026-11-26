"""Titles for a person's question, or like a title they chose: the model proposes, TMDB
says which of them are real."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TypedDict

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient, year_of
from mediaman.services.openai.client import _title_hostile, ask_openai
from mediaman.services.openai.recommendations.enrich import _exact_result
from mediaman.services.openai.recommendations.prompts import strip_season_suffix
from mediaman.services.openai.recommendations.reasons import names_any
from mediaman.services.watch_catalog import (
    TitleNotFound,
    TmdbUnavailable,
    details,
    normalise_title,
)

from .taste import Taste, checked_line, clean_list, clean_text, data_block, words

_ASK_FOR = 40
MAX_TITLES = 36
#: TMDB checks of one answer at once, each a search and the details of what it found. At 1
#: they run one after another in the run's own thread.
CHECKERS = 6
_MAX_CHIPS = 4
_CHIP_LEN = 32
_REASON_LEN = 140
_TITLE_LEN = 200
# Seconds the TMDB checks of one answer may take, and failures before it is left alone.
_CHECK_S = 30.0
_FAILURES_MAX = 2
# How long the wait for a check lasts before a stop is looked for again.
_POLL_S = 0.25
# A question about the asker ("films I would like", "something like what we watched").
_PERSONAL_RE = re.compile(r"\b(?:i|me|my|mine|myself|we|our|ours)\b", re.IGNORECASE)
# "Because you liked Alibi: ...": a claim of what the person liked, or else played. A title
# may hold ": " too.
_LEAD_RE = re.compile(r"^because you(?:'ve| have)? (?P<how>\w+) ", re.IGNORECASE)
_LIKED = frozenset({"liked", "loved", "enjoyed", "adored", "rated"})
_COLON_RE = re.compile(r"\s*:\s+")


def is_personal(text: str) -> bool:
    return bool(_PERSONAL_RE.search(text))


@dataclass(frozen=True, slots=True)
class Question:
    """What is asked: *text* in the person's words, or titles like *like*. *refine* is a chip
    of the answer whose titles are *narrowing*."""

    text: str = ""
    like: str = ""
    like_year: int | None = None
    like_key: tuple[str, int | None] | None = None
    refine: str = ""
    narrowing: tuple[str, ...] = ()
    #: Titles left out of the answer by (kind, TMDB id): of a personal question, those played.
    skip: frozenset[tuple[str, int]] = frozenset()


class AnswerTitle(TypedDict):
    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    poster_path: str | None
    vote_average: float | None
    minutes: int | None
    season_count: int | None
    genre: str | None
    reason: str


class AssistAnswer(TypedDict):
    titles: list[AnswerTitle]
    chips: list[str]
    instead: list[str]


_RULES = f"""Answer with up to {_ASK_FOR} titles, the best fit first.
- Only real, released movies and TV shows that you are sure exist. Never invent one to fill a gap.
- Every title must fit all of what is asked. Fewer titles, or none, is better than a poor fit.
- No adult titles. At most two of one franchise. Nothing under "disliked" and little like it.
- "reason" is one plain British English sentence of at most 120 characters that says how this title fits what is asked, specific to it. No hype, no plot past the set-up, and nothing about the reader.
- Also give 3 or 4 chips: short ways (2 to 4 words) to narrow this answer, such as "Under 100 minutes" or "Funnier".
- If no title fits, give instead 2 or 3 simpler searches, each the same wish with one part dropped.
Return one JSON object and nothing else:
{{"items":[{{"kind":"title","title":"official English title","year":2017,"media_type":"movie","reason":"..."}},{{"kind":"chip","text":"..."}},{{"kind":"instead","text":"..."}}]}}
"media_type" is "movie" or "tv". "year" is the first release year."""


def _prompt(question: Question, taste: Taste) -> str:
    data: dict[str, object] = {**taste.fields()}
    personal = not question.like and bool(data) and is_personal(question.text)
    if question.like:
        data["title"] = clean_list([question.like])
        data["year"] = question.like_year
        wish = 'Find titles like the one under "title", in what makes it itself. Leave it out.'
    elif personal:
        data["question"] = clean_text(question.text)
        wish = (
            'Find movies and TV shows for the wish under "question". It is about the asker: '
            'build the answer from their own lists, newest first. Follow what is under "liked" '
            'and "played"; stay away from what is under "disliked" and "abandoned". Leave out '
            'every title under "played". Here a "reason" may speak of the reader: each starts '
            'with the title of theirs it follows from, as "Because you liked <title>:" for one '
            'under "liked" or "Because you watched <title>:" for one under "played".'
        )
    else:
        data["question"] = clean_text(question.text)
        wish = 'Find movies and TV shows for the wish under "question".'
    if question.refine:
        data["narrow_to"] = clean_list([question.refine])
        data["answered"] = clean_list(question.narrowing)
        wish += (
            ' It was answered with "answered". Narrow it to "narrow_to": keep those that fit '
            "and add others that do."
        )
    if taste.fields() and not personal:
        wish += " The other lists are the asker's own taste: use them only to choose between fits."
    return f"{wish}\n{data_block(data)}\n{_RULES}"


def _texts(items: list[dict[str, object]], kind: str, max_len: int) -> list[str]:
    found: dict[str, str] = {}
    for item in items:
        if item.get("kind") == kind and (text := checked_line(item.get("text"), max_len)):
            found.setdefault(text.casefold(), text)
    return list(found.values())[:_MAX_CHIPS]


def _year(value: object) -> int | None:
    valid = isinstance(value, int) and not isinstance(value, bool) and 1880 <= value <= 2100
    return value if valid else None  # type: ignore[return-value]


def make_answer(
    conn: sqlite3.Connection,
    client: TmdbClient,
    question: Question,
    taste: Callable[[], Taste],
    *,
    secret_key: str,
    stopped: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    stage: Callable[[str], None] = lambda _: None,
    found_so_far: Callable[[list[AnswerTitle]], None] = lambda _: None,
) -> AssistAnswer | None:
    """The answer to *question*; None when TMDB could not be asked, or it was *stopped*.
    No titles at all means nothing real fits. Raises OpenAIUnavailable when the model gave
    no answer. *stage* is told each step as it begins: "taste", "choosing", "checking".
    *found_so_far* is told the titles checked so far, in the model's order, as they come."""
    stage("taste")
    known = taste()
    prompt = _prompt(question, known)
    if stopped():
        return None
    stage("choosing")
    items = ask_openai(prompt, conn, secret_key=secret_key)
    if stopped():
        return None
    stage("checking")
    ends_at = clock() + _CHECK_S
    named = [i for i in items[: _ASK_FOR * 2] if i.get("kind", "title") == "title"][:_ASK_FOR]
    waiting = list(enumerate(named))
    going: dict[Future[AnswerTitle | None], int] = {}
    checked: dict[int, AnswerTitle] = {}
    failures = 0
    pool = (
        ThreadPoolExecutor(max_workers=CHECKERS, thread_name_prefix="assist-check")
        if CHECKERS > 1
        else None
    )

    def start(item: dict[str, object]) -> Future[AnswerTitle | None]:
        if pool is not None:
            return pool.submit(_checked, client, item, question)
        done: Future[AnswerTitle | None] = Future()
        try:
            done.set_result(_checked(client, item, question))
        except Exception as exc:  # rationale: raised again where the result is read
            done.set_exception(exc)
        return done

    try:
        while (
            failures < _FAILURES_MAX and len(_in_order(checked)) < MAX_TITLES and clock() < ends_at
        ):
            if stopped():
                return None
            while waiting and len(going) < max(CHECKERS, 1):
                at, item = waiting.pop(0)
                going[start(item)] = at
            if not going:
                break
            timeout = min(_POLL_S, max(0.0, ends_at - clock()))
            done, _ = wait(going, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                at = going.pop(future)
                try:
                    title = future.result()
                except (SafeHTTPError, requests.RequestException, ValueError, TmdbUnavailable):
                    # Counted as checks end, so the first two to fail end it, whichever they are.
                    failures += 1
                    continue
                if title is None or (title["media_type"], title["tmdb_id"]) in question.skip:
                    continue
                reason = _honest(title["reason"], known)
                if reason:
                    title["reason"] = reason
                    checked[at] = title
            if done:
                found_so_far(_in_order(checked))
    finally:
        if pool is not None:
            # A check still running ends on its own; nothing waits for it.
            pool.shutdown(wait=False, cancel_futures=True)
    titles = _in_order(checked)[:MAX_TITLES]
    if failures and not titles:
        return None
    return AssistAnswer(
        titles=titles,
        chips=_texts(items, "chip", _CHIP_LEN) if titles else [],
        instead=[] if titles else _texts(items, "instead", 60),
    )


def _in_order(checked: dict[int, AnswerTitle]) -> list[AnswerTitle]:
    """The titles found, in the order the model gave them, each once."""
    seen: dict[tuple[str, int], AnswerTitle] = {}
    for at in sorted(checked):
        seen.setdefault((checked[at]["media_type"], checked[at]["tmdb_id"]), checked[at])
    return list(seen.values())


def _honest(reason: str, taste: Taste) -> str:
    """*reason* without a "Because you liked <title>:" that names no title of the person's
    own: one they liked for "liked", one they played for "watched" and the like. Empty when
    nothing is left of it."""
    lead = _LEAD_RE.match(reason)
    if lead is None:
        return reason
    theirs = taste.liked if lead["how"].casefold() in _LIKED else taste.played
    after = reason[lead.end() :]
    colons = list(_COLON_RE.finditer(after))
    if not colons:
        # Not the shape asked for: the claim runs through the whole of it.
        return reason if names_any(reason, theirs) else ""
    if any(_is_one_of(after[: c.start()], theirs) for c in colons):
        return reason
    rest = after[colons[-1].end() :]
    return rest[:1].upper() + rest[1:] if words(rest) else ""


def _is_one_of(named: str, titles: Sequence[str]) -> bool:
    """Whether *named* is one of *titles*, as a reason would spell it: each names the other."""
    # Not where a sentence begins, where a title of one word names nothing.
    said = "Because of {}"
    return any(
        names_any(said.format(named), [title]) and names_any(said.format(title), [named])
        for title in titles
    )


def _checked(client: TmdbClient, item: dict[str, object], question: Question) -> AnswerTitle | None:
    """The title the model named as TMDB knows it; None when it is not real, is the one the
    others should be like, or has no reason that may be shown."""
    # Only what TMDB is asked for: the name shown is TMDB's.
    name, year = str(item.get("title") or "").strip(), _year(item.get("year"))
    reason = checked_line(item.get("reason"), _REASON_LEN)
    kind = item.get("media_type")
    if len(name) > _TITLE_LEN or _title_hostile(name) or not reason or kind not in ("movie", "tv"):
        return None
    kind = str(kind)
    if kind == "tv":
        name = strip_season_suffix(name) or name
    best = None
    for asked_year in dict.fromkeys((year, None)):
        results = client.search_results_or_raise(name, year=asked_year, media_type=kind)
        best = _exact_result(results, name, year)
        if best is not None:
            break
    if best is None:
        return None
    tmdb_id = best.get("id")
    released = year_of(best.get("release_date") or best.get("first_air_date"))
    # Of two titles of one name the reason was written for one.
    if not isinstance(tmdb_id, int) or (year and released and abs(year - released) > 1):
        return None
    title = str(best.get("title") or best.get("name") or "")
    if question.like_key is not None:
        like_kind, like_id = question.like_key
        if kind == like_kind and (
            tmdb_id == like_id or normalise_title(title) == normalise_title(question.like)
        ):
            return None
    try:
        # Not behind the gate of requests that wait on TMDB: nobody's request waits here.
        meta = details(client, kind, tmdb_id, background=True)
    except TitleNotFound:
        return None
    poster, vote = best.get("poster_path"), best.get("vote_average")
    return AnswerTitle(
        media_type=kind,
        tmdb_id=tmdb_id,
        title=title,
        year=released,
        poster_path=poster if isinstance(poster, str) else None,
        vote_average=float(vote) if isinstance(vote, int | float) else None,
        minutes=meta["runtime_min"],
        season_count=meta["season_count"],
        genre=next(iter(meta["genres"]), None),
        reason=reason,
    )
