"""The small posters of a person's titles: their title's shading and clamp, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

CSS = (
    Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css/_watch_cast.css"
).read_text()


def _poster_title_rule() -> str:
    rule = re.search(r"\.cast-card__poster > \.cast-card__poster-name \{([^}]*)\}", CSS)
    assert rule, "no poster title rule"
    return rule.group(1)


def test_a_poster_title_s_shading_reaches_under_its_transparent_border() -> None:
    rule = _poster_title_rule()
    assert "border-bottom: 4px solid transparent" in rule
    # Without it the gradient tiles into the border and a lighter strip shows at the poster's foot.
    assert "background-origin: border-box" in rule


def test_a_poster_title_is_held_to_three_lines() -> None:
    rule = _poster_title_rule()
    assert "-webkit-line-clamp: 3" in rule
    assert "line-clamp: 3" in rule
