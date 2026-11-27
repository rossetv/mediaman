"""The title overlay's genre link reaches a finger's size without outgrowing the row that clips it."""

from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
DETAIL = (STYLES / "_watch_detail.css").read_text()


def test_a_genre_link_reaches_44_px_from_its_line() -> None:
    rule = re.search(r":is\(\.ttl__genre, \.ttl__director\)::after \{([^}]*)\}", DETAIL)
    assert rule and "inset: -14px -3px" in rule.group(1)
    # The row clips what hangs past 14 px, so no more than that.
    assert "clip-path: inset(-14px -20px -14px var(--dot))" in DETAIL
