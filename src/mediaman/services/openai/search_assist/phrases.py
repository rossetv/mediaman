"""Six search phrases made for one person."""

from __future__ import annotations

import sqlite3

from mediaman.services.openai.client import call_openai

from .taste import Taste, checked_line, data_block

#: Shown to someone without phrases of their own.
FIXED_PHRASES = ("popular action movies", "popular comedy movies", "top rated sci-fi")
PHRASES = 6
# Fewer good ones than this is no better than the fixed ones.
_MIN_PHRASES = 3
_MAX_LEN = 60

_RULES = f"""Write {PHRASES + 2} short search phrases this person might like to try next.
- Each is 2 to 8 words, lower case but for names, with no quotes and no full stop.
- Each is a different idea: a mood, a maker, a premise, a length, a place, a twist on a genre.
- Build on what they played, liked and searched for. Never repeat a search word for word.
- Nothing like what is under "disliked".
- Never mention age, sex, family, beliefs or health. No adult themes.
Return one JSON object and nothing else: {{"items":[{{"phrase":"..."}}]}}"""


def make_phrases(conn: sqlite3.Connection, taste: Taste, *, secret_key: str) -> list[str]:
    """Up to six phrases from *taste*; [] when the model gave too few that may be shown."""
    items = call_openai(data_block({**taste.fields()}) + "\n" + _RULES, conn, secret_key=secret_key)
    searched = {s.casefold() for s in taste.searches}
    found: dict[str, str] = {}
    for item in items:
        phrase = checked_line(item.get("phrase"), _MAX_LEN)
        if phrase and len(phrase.split()) >= 2 and phrase.casefold() not in searched:
            found.setdefault(phrase.casefold(), phrase)
    return list(found.values())[:PHRASES] if len(found) >= _MIN_PHRASES else []
