"""Titles for a person's question, or like a title they chose: the model proposes, TMDB
says which of them are real."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.tmdb import TmdbClient, year_of
from mediaman.services.openai.client import _title_hostile, ask_openai
from mediaman.services.openai.recommendations.enrich import _exact_result
from mediaman.services.openai.recommendations.prompts import strip_season_suffix
from mediaman.services.watch_catalog import (
    TitleNotFound,
    TmdbUnavailable,
    details,
    normalise_title,
)

from .taste import Taste, checked_line, clean_list, clean_text, data_block

_ASK_FOR = 16
MAX_TITLES = 10
_MAX_CHIPS = 4
_CHIP_LEN = 32
_REASON_LEN = 140
_TITLE_LEN = 200
# Seconds the TMDB checks of one answer may take, and failures before it is left alone.
_CHECK_S = 30.0
_FAILURES_MAX = 2


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
    if question.like:
        data["title"] = clean_list([question.like])
        data["year"] = question.like_year
        wish = 'Find titles like the one under "title", in what makes it itself. Leave it out.'
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
    if taste.fields():
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
) -> AssistAnswer | None:
    """The answer to *question*; None when TMDB could not be asked, or it was *stopped*.
    No titles at all means nothing real fits. Raises OpenAIUnavailable when the model gave
    no answer."""
    prompt = _prompt(question, taste())
    if stopped():
        return None
    items = ask_openai(prompt, conn, secret_key=secret_key)
    if stopped():
        return None
    ends_at = clock() + _CHECK_S
    titles: dict[tuple[str, int], AnswerTitle] = {}
    failures = 0
    for item in items[: _ASK_FOR * 2]:
        if len(titles) >= MAX_TITLES or failures >= _FAILURES_MAX or clock() >= ends_at:
            break
        if item.get("kind", "title") != "title":
            continue
        if stopped():
            return None
        try:
            found = _checked(client, item, question)
        except (SafeHTTPError, requests.RequestException, ValueError, TmdbUnavailable):
            failures += 1
            continue
        if found is not None:
            titles.setdefault((found["media_type"], found["tmdb_id"]), found)
    if failures and not titles:
        return None
    return AssistAnswer(
        titles=list(titles.values()),
        chips=_texts(items, "chip", _CHIP_LEN) if titles else [],
        instead=[] if titles else _texts(items, "instead", 60),
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
