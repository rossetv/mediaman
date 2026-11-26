"""The title overlay's stylesheets where a narrow screen or a finger decides: the genre link's reach,
an episode's facts and the cast's columns."""

from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
DETAIL = (STYLES / "_watch_detail.css").read_text()
CAST = (STYLES / "_watch_cast.css").read_text()


def test_a_genre_link_reaches_44_px_from_its_line() -> None:
    rule = re.search(r":is\(\.ttl__genre, \.ttl__director\)::after \{([^}]*)\}", DETAIL)
    assert rule and "inset: -14px -3px" in rule.group(1)
    # The row clips what hangs past 14 px, so no more than that.
    assert "clip-path: inset(-14px -20px -14px var(--dot))" in DETAIL


def test_an_episodes_dot_ends_the_fact_before_it_at_every_width() -> None:
    rule = re.search(r"\n(\.ttl \.watch-ep__fact:not\(:last-child\)::after) \{([^}]*)\}", DETAIL)
    assert rule, "no dot rule outside a media query"
    # A no-break space keeps it from standing alone on a line.
    assert r'content: "\00a0·"' in rule.group(2)
    assert "::before" not in DETAIL.split(".ttl .watch-ep__fact", 1)[1].split("}", 1)[0]


def test_the_cast_has_three_columns_where_four_would_break_a_name() -> None:
    block = re.search(r"@media \(max-width: 359\.98px\) \{([^}]*\{[^}]*\})", CAST)
    assert block and "repeat(3, minmax(0, 1fr))" in block.group(1)
